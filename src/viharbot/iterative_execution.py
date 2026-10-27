"""Bounded, iterative orchestration with application-side authorization.

Charlie proposes one action at a time. The application authorizes every action, runs approved
capabilities on the existing services through the same validated boundary as the fixed
pipeline, and decides when the task may complete. This is controlled coordination, not
unrestricted autonomy: explicit loop, explicit budgets, no parallelism, no retries.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

from pydantic import BaseModel, ConfigDict

from . import demo, orchestration_planner
from .deadline import DeadlineExceeded, RequestDeadline
from .delegated_execution import delegation_enabled
from .delegation import DelegationError, PlannerInferenceError
from .demo import AgentPipelineError, invoke_agent
from .execution_bindings import (
    ExecutionBindingSet,
    default_bindings,
    load_bindings,
)
from .identity import Capability
from .identity_registry import AgentRegistry, default_registry
from .orchestration_planner import DecisionContext
from .orchestration_policy import (
    ActionRejected,
    OrchestrationAction,
    allowed_capabilities,
    authorize_action,
    build_payload,
    check_completion,
    fresh_latest,
    fresh_results,
    input_versions,
)
from .orchestration_state import (
    CANONICAL_ORDER,
    LEGACY_KEYS,
    DecisionRecord,
    ExecutionBudget,
    OrchestrationState,
    StepResult,
    rejection_notice,
)
from .pipeline_state import AgentActivity, PipelineContext, PipelineState

ITERATIVE_FLAG = "VIHARBOT_ITERATIVE_ORCHESTRATION_ENABLED"
logger = logging.getLogger("orchestrator")


def iterative_orchestration_enabled() -> bool:
    """Needs its own flag plus delegation (which itself needs identity routing)."""
    own = os.getenv(ITERATIVE_FLAG, "false").strip().lower() in {"1", "true", "yes"}
    return own and delegation_enabled()


class OrchestrationFailed(DelegationError):
    """Orchestration ended without the results /api/chat requires. `code` is content-free."""

    def __init__(self, code: str) -> None:
        super().__init__("orchestration did not complete", code)
        self.code = code


class IterativePipelineResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    state: PipelineState
    orchestration: OrchestrationState

    def to_legacy_dict(self) -> dict:
        """The same result dictionary run_pipeline() returns."""
        return self.state.to_legacy_dict()


def _log(level: int, message: str, state: OrchestrationState, clock, deadline: RequestDeadline,
         **fields: object) -> None:
    logger.log(
        level,
        message,
        extra={
            "agent": "orchestrator",
            "request_id": state.request_id,
            "planner_identity": "charlie",
            "iteration": state.iteration,
            "decision_number": state.decision_number,
            "remaining_budget": state.remaining(clock),
            "elapsed_ms": round((clock() - state.started_at) * 1000, 1),
            "remaining_ms": round(max(deadline.remaining(), 0.0) * 1000, 1),
            **fields,
        },
    )


def _final_state(state: OrchestrationState, activity: list[AgentActivity]) -> tuple[PipelineState, int]:
    """Project the fresh results onto the existing PipelineState.

    A result built on outdated upstream evidence is left out and nothing replaces it. The
    returned count is how many capabilities were excluded as stale. `activity` still lists
    everything that actually ran.
    """
    fresh = fresh_results(state)
    excluded = sum(
        1 for c in CANONICAL_ORDER if state.latest(c) is not None and c not in fresh
    )

    def domain(capability: Capability):
        step = fresh.get(capability)
        return step.domain if step is not None else None

    context = PipelineContext(
        research=domain(Capability.RESEARCH),
        fact_check=domain(Capability.FACT_CHECK),
        source_quality=domain(Capability.SOURCE_QUALITY),
        security=domain(Capability.SECURITY_ASSESSMENT),
        privacy_review=domain(Capability.PRIVACY_REVIEW),
    )
    return (
        PipelineState(
            request_id=state.request_id,
            context=context,
            synthesis=domain(Capability.SYNTHESIS),
            responses={LEGACY_KEYS[c]: step.envelope for c, step in fresh.items()},
            agent_activity=activity,
        ),
        excluded,
    )


def run_iterative_pipeline(
    question: str,
    request_id: str,
    *,
    research_url: str,
    security_url: str,
    specialist_urls: dict[str, str] | None = None,
    send: Callable[[str, dict, str, str], dict] | None = None,
    decide: Callable[[OrchestrationState, DecisionContext], OrchestrationAction] | None = None,
    registry: AgentRegistry | None = None,
    bindings: ExecutionBindingSet | None = None,
    budget: ExecutionBudget | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> IterativePipelineResult:
    registry = registry or default_registry()
    if bindings is None:
        bindings = default_bindings() if registry is default_registry() else load_bindings(registry)
    decide = decide or orchestration_planner.decide_next_action
    send = send or demo.post_json
    budget = budget or ExecutionBudget.from_env()

    # One absolute deadline for the whole request. It is never reset or extended.
    started = clock()
    deadline = RequestDeadline(expires_at=started + budget.max_seconds, clock=clock)

    urls = {"research": research_url, "security": security_url}
    urls.update({key.replace("_", "-"): url for key, url in (specialist_urls or {}).items() if url})
    state = OrchestrationState(
        request_id=request_id,
        original_question=question,
        execution_budget=budget,
        started_at=started,
    )
    activity: list[AgentActivity] = []
    notice = None

    def log(level: int, message: str, **fields: object) -> None:
        _log(level, message, state, clock, deadline, **fields)

    def complete(kind: str, reason: str, status: str) -> IterativePipelineResult:
        state.status = status  # type: ignore[assignment]
        state.completion_kind = kind  # type: ignore[assignment]
        state.completion_reason = reason
        state.elapsed_ms = round((clock() - started) * 1000, 1)
        pipeline_state, excluded = _final_state(state, activity)
        log(logging.INFO, "orchestration completed", action_status="completed",
            completion_status=kind, completion_reason=reason, stale_excluded=excluded)
        return IterativePipelineResult(state=pipeline_state, orchestration=state)

    def fail(reason: str):
        state.status = "failed"
        state.completion_kind = "incomplete"
        state.completion_reason = reason
        error = "DeadlineExceeded" if reason == "time_budget" else reason
        log(logging.ERROR, "orchestration failed", action_status="failed",
            completion_status="incomplete", completion_reason=reason, error=error)
        if reason == "time_budget":
            raise DeadlineExceeded("request deadline exceeded")
        raise OrchestrationFailed(reason)

    def by_policy(reason: str) -> IterativePipelineResult:
        """A limit was reached. The application completes only if the required results are
        present and fresh; this is never recorded as Charlie's decision."""
        if check_completion(state) is None:
            return complete("policy_budget", reason, "budget_exhausted")
        return fail(reason)

    def reject(action: OrchestrationAction, code: str) -> None:
        state.rejected_actions += 1
        state.record(DecisionRecord(
            decision_number=state.decision_number, requested_action=action.action,
            capability=Capability(action.capability) if action.capability else None,
            status="rejected", reason_code=code))
        log(logging.WARNING, "action rejected", action_status="rejected",
            requested_action=action.action, capability=action.capability, error=code)

    while True:
        if deadline.expired():
            return by_policy("time_budget")
        if state.decision_number >= budget.max_decisions:
            return by_policy("decision_budget")
        # When the execution budget is used up, Charlie still gets one final evaluation of
        # the last result, but only `finish` is accepted. No further delegation is possible.
        final_evaluation = len(state.completed_steps) >= budget.max_executions
        if final_evaluation and check_completion(state) is not None:
            return by_policy("execution_budget")

        state.decision_number += 1
        allowed = () if final_evaluation else allowed_capabilities(
            state, registry, bindings, set(urls), seconds_left=deadline.remaining())
        context = DecisionContext(
            notice=notice,
            allowed_capabilities=allowed,
            finish_allowed=check_completion(state) is None,
            deadline=deadline,
            final_evaluation=final_evaluation,
        )
        try:
            action = decide(state, context)  # exactly one planning call per decision
        except DeadlineExceeded:
            log(logging.ERROR, "orchestration planner timed out", action_status="planner_failed",
                error="DeadlineExceeded")
            return by_policy("time_budget")
        except DelegationError as exc:
            state.status = "failed"
            log(logging.ERROR, "orchestration planner failed", action_status="planner_failed",
                error=exc.cause_type or type(exc).__name__)
            raise
        except Exception as exc:
            state.status = "failed"
            log(logging.ERROR, "orchestration planner failed", action_status="planner_failed",
                error=type(exc).__name__)
            raise PlannerInferenceError("planning inference failed", type(exc).__name__) from None
        notice = None
        if deadline.expired():
            # Charlie answered after the deadline. Nothing he said is acted on, and a late
            # `finish` is not recorded as his approval.
            state.record(DecisionRecord(
                decision_number=state.decision_number, requested_action=action.action,
                capability=Capability(action.capability) if action.capability else None,
                status="rejected", reason_code="deadline_exceeded"))
            log(logging.WARNING, "late planning result discarded", action_status="rejected",
                requested_action=action.action, error="DeadlineExceeded")
            return by_policy("time_budget")
        log(logging.INFO, "action recommended", action_status="recommended",
            requested_action=action.action, capability=action.capability)

        rejection: str | None = None
        authorized = None
        if action.action == "finish":
            rejection = check_completion(state)
            if rejection is None:
                if deadline.expired():  # last check before accepting normal completion
                    return by_policy("time_budget")
                state.record(DecisionRecord(decision_number=state.decision_number,
                                            requested_action="finish", status="completed"))
                return complete("charlie_approved", "charlie_finished", "completed")
        elif final_evaluation:
            rejection = "execution_budget"
        else:
            try:
                # Recomputed now: planning may have taken a long time.
                authorized = authorize_action(action, state, registry, bindings, set(urls),
                                              seconds_left=deadline.remaining())
            except ActionRejected as exc:
                rejection = exc.code

        if rejection is not None:
            reject(action, rejection)
            if final_evaluation:
                return by_policy("execution_budget")  # Charlie did not finish; policy decides
            if state.rejected_actions >= budget.max_rejected_actions:
                return by_policy("rejection_limit")
            notice = rejection_notice(rejection)
            continue

        # Authorized delegation: run exactly one capability on its approved service.
        assert authorized is not None
        log(logging.INFO, "action authorized", action_status="authorized",
            requested_action="delegate", capability=authorized.capability.value,
            identity_id=authorized.identity_id, service_name=authorized.service_name)
        state.iteration += 1
        state.add_gap(action.unresolved_question)
        payload = build_payload(authorized.capability, state)
        based_on = input_versions(authorized.capability, state)
        call_started = time.perf_counter()
        try:
            envelope, domain = invoke_agent(
                send, urls[authorized.service_name], payload, request_id,
                authorized.service_name, activity, deadline=deadline)
        except DeadlineExceeded:
            state.record(DecisionRecord(
                decision_number=state.decision_number, requested_action="delegate",
                capability=authorized.capability, status="failed",
                reason_code="deadline_exceeded"))
            log(logging.ERROR, "delegated execution timed out", action_status="failed",
                capability=authorized.capability.value, identity_id=authorized.identity_id,
                service_name=authorized.service_name, error="DeadlineExceeded")
            return by_policy("time_budget")
        except AgentPipelineError:
            state.status = "failed"
            state.record(DecisionRecord(
                decision_number=state.decision_number, requested_action="delegate",
                capability=authorized.capability, status="failed", reason_code="agent_failed"))
            log(logging.ERROR, "delegated execution failed", action_status="failed",
                capability=authorized.capability.value, identity_id=authorized.identity_id,
                service_name=authorized.service_name, error="AgentPipelineError")
            raise
        duration_ms = round((time.perf_counter() - call_started) * 1000, 2)
        step = StepResult(
            capability=authorized.capability,
            version=state.run_count(authorized.capability) + 1,
            identity_id=authorized.identity_id,
            service_name=authorized.service_name,
            decision_number=state.decision_number,
            duration_ms=duration_ms,
            envelope=envelope,
            domain=domain,
            based_on=based_on,
        )
        state.completed_steps.append(step)
        state.record(DecisionRecord(
            decision_number=state.decision_number, requested_action="delegate",
            capability=authorized.capability, status="executed", duration_ms=duration_ms))
        # "executed" is only claimed here, after a validated, on-time response arrived.
        log(logging.INFO, "delegated execution completed", action_status="executed",
            capability=authorized.capability.value, identity_id=authorized.identity_id,
            service_name=authorized.service_name,
            model_alias=envelope.requested_model_alias or None, execution_duration_ms=duration_ms)
