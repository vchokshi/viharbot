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


def _log(level: int, message: str, state: OrchestrationState, clock, **fields: object) -> None:
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
            **fields,
        },
    )


def _final_state(state: OrchestrationState, activity: list[AgentActivity]) -> PipelineState:
    """Project the latest validated result per capability onto the existing PipelineState."""
    latest = {c: state.latest(c) for c in CANONICAL_ORDER}
    context = PipelineContext(
        research=latest[Capability.RESEARCH].domain,
        fact_check=latest[Capability.FACT_CHECK].domain if latest[Capability.FACT_CHECK] else None,
        source_quality=(
            latest[Capability.SOURCE_QUALITY].domain if latest[Capability.SOURCE_QUALITY] else None
        ),
        security=latest[Capability.SECURITY_ASSESSMENT].domain,
        privacy_review=(
            latest[Capability.PRIVACY_REVIEW].domain if latest[Capability.PRIVACY_REVIEW] else None
        ),
    )
    synthesis = latest[Capability.SYNTHESIS]
    return PipelineState(
        request_id=state.request_id,
        context=context,
        synthesis=synthesis.domain if synthesis else None,
        responses={LEGACY_KEYS[c]: step.envelope for c, step in latest.items() if step is not None},
        agent_activity=activity,
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

    urls = {"research": research_url, "security": security_url}
    urls.update({key.replace("_", "-"): url for key, url in (specialist_urls or {}).items() if url})
    state = OrchestrationState(
        request_id=request_id,
        original_question=question,
        execution_budget=budget or ExecutionBudget.from_env(),
        started_at=clock(),
    )
    activity: list[AgentActivity] = []
    notice = None

    def finish_with(status: str, completion: str) -> IterativePipelineResult:
        state.status = status  # type: ignore[assignment]
        _log(logging.INFO, "orchestration completed", state, clock,
             action_status="completed", completion_status=completion)
        return IterativePipelineResult(state=_final_state(state, activity), orchestration=state)

    def terminate(reason: str) -> IterativePipelineResult:
        """Budget or loop limit reached. Complete only if the mandatory results are valid."""
        if check_completion(state) is None:
            return finish_with("budget_exhausted", reason)
        state.status = "failed"
        _log(logging.ERROR, "orchestration failed", state, clock,
             action_status="failed", completion_status=reason, error=reason)
        raise OrchestrationFailed(reason)

    while True:
        budget_now = state.execution_budget
        if state.decision_number >= budget_now.max_decisions:
            return terminate("decision_budget")
        if state.elapsed(clock) >= budget_now.max_seconds:
            return terminate("time_budget")
        if len(state.completed_steps) >= budget_now.max_executions:
            return terminate("execution_budget")

        seconds_left = budget_now.max_seconds - state.elapsed(clock)
        state.decision_number += 1
        allowed = allowed_capabilities(state, registry, bindings, set(urls), seconds_left=seconds_left)
        context = DecisionContext(
            notice=notice,
            allowed_capabilities=allowed,
            finish_allowed=check_completion(state) is None,
            timeout_seconds=seconds_left,
        )
        try:
            action = decide(state, context)  # exactly one planning call per decision
        except DelegationError as exc:
            state.status = "failed"
            _log(logging.ERROR, "orchestration planner failed", state, clock,
                 action_status="planner_failed", error=exc.cause_type or type(exc).__name__)
            raise
        except Exception as exc:
            state.status = "failed"
            _log(logging.ERROR, "orchestration planner failed", state, clock,
                 action_status="planner_failed", error=type(exc).__name__)
            raise PlannerInferenceError("planning inference failed", type(exc).__name__) from None
        notice = None
        _log(logging.INFO, "action recommended", state, clock, action_status="recommended",
             requested_action=action.action, capability=action.capability)

        rejection: str | None = None
        authorized = None
        if action.action == "finish":
            rejection = check_completion(state)
            if rejection is None:
                state.record(DecisionRecord(decision_number=state.decision_number,
                                            requested_action="finish", status="completed"))
                return finish_with("completed", "finished")
        else:
            try:
                authorized = authorize_action(action, state, registry, bindings, set(urls),
                                              seconds_left=seconds_left)
            except ActionRejected as exc:
                rejection = exc.code

        if rejection is not None:
            state.rejected_actions += 1
            state.record(DecisionRecord(
                decision_number=state.decision_number, requested_action=action.action,
                capability=Capability(action.capability) if action.capability else None,
                status="rejected", reason_code=rejection))
            _log(logging.WARNING, "action rejected", state, clock, action_status="rejected",
                 requested_action=action.action, capability=action.capability, error=rejection)
            if state.rejected_actions >= budget_now.max_rejected_actions:
                return terminate("rejection_limit")
            notice = rejection_notice(rejection)
            continue

        # Authorized delegation: run exactly one capability on its approved service.
        assert authorized is not None
        _log(logging.INFO, "action authorized", state, clock, action_status="authorized",
             requested_action="delegate", capability=authorized.capability.value,
             identity_id=authorized.identity_id, service_name=authorized.service_name)
        state.iteration += 1
        state.add_gap(action.unresolved_question)
        payload = build_payload(authorized.capability, state)
        based_on = input_versions(authorized.capability, state)
        started = time.perf_counter()
        try:
            envelope, domain = invoke_agent(
                send, urls[authorized.service_name], payload, request_id,
                authorized.service_name, activity)
        except AgentPipelineError:
            state.status = "failed"
            state.record(DecisionRecord(
                decision_number=state.decision_number, requested_action="delegate",
                capability=authorized.capability, status="failed", reason_code="agent_failed"))
            _log(logging.ERROR, "delegated execution failed", state, clock,
                 action_status="failed", capability=authorized.capability.value,
                 identity_id=authorized.identity_id, service_name=authorized.service_name,
                 error="AgentPipelineError")
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
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
        # "executed" is only claimed here, after the validated response arrived.
        _log(logging.INFO, "delegated execution completed", state, clock,
             action_status="executed", capability=authorized.capability.value,
             identity_id=authorized.identity_id, service_name=authorized.service_name,
             model_alias=envelope.requested_model_alias or None, execution_duration_ms=duration_ms)
