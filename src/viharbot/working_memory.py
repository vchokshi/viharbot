"""Read-only, bounded projection of Charlie's per-request working memory.

`OrchestrationState` lives only in memory for one request and is never serialized. This module
adds one thing: when the Agent Memory Explorer is enabled, the web process keeps a *reference* to
the live state (plus the last few finished ones) so a founder-authorized API can project it. Nothing
is written to disk or to any database; a restart releases everything.

The projection is an explicit allowlist, versioned, and split into two tiers:

* metadata: counts, identities, capabilities, versions, durations, reason codes, budgets. No text
  that a user or a model wrote.
* content: the question, the current goal, evidence summaries and pending gaps. Included only when
  the caller holds the separate content permission. All of it is untrusted data.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .orchestration_policy import fresh_results
from .orchestration_state import (
    MAX_DECISION_HISTORY,
    OrchestrationState,
    build_observation,
)

SCHEMA = "viharbot.working-memory/1"
ENABLED_FLAG = "MEMORY_EXPLORER_ENABLED"
MAX_FINISHED = 5  # finished runs kept in memory
MAX_ACTIVE = 16  # a hard cap on live references, so a leak cannot grow without bound
RELEASE_GRACE_SECONDS = 30.0
MAX_QUESTION_CHARS = 500
MAX_STEPS = 16


def explorer_enabled() -> bool:
    return os.getenv(ENABLED_FLAG, "false").strip().lower() in {"1", "true", "yes"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BudgetView(Strict):
    max_decisions: int
    max_executions: int
    max_repeats_per_capability: int
    max_seconds: float
    max_rejected_actions: int
    remaining_decisions: int
    remaining_executions: int
    remaining_seconds: float


class StepView(Strict):
    sequence: int
    identity: str
    capability: str
    service: str
    version: int
    decision_number: int
    duration_ms: float
    based_on: dict[str, int]
    # False when newer evidence superseded this result (it is left out of the final answer).
    current: bool
    # Content tier only.
    summary: str | None = None
    evidence_gaps: tuple[str, ...] | None = None


class DecisionView(Strict):
    decision_number: int
    requested_action: str
    capability: str | None
    status: str
    reason_code: str | None
    duration_ms: float | None


class ContentView(Strict):
    original_question: str
    current_goal: str
    pending_gaps: tuple[str, ...]


class WorkingMemoryRun(Strict):
    request_id: str
    # active: still running. completed: finished, snapshot kept in memory.
    # released: it never reported an end and its time budget has passed.
    lifecycle: Literal["active", "completed", "released"]
    status: str
    iteration: int
    decision_count: int
    completion_kind: str | None
    completion_reason: str | None
    elapsed_ms: float | None
    rejected_actions: int
    pending_gap_count: int
    peer_review_count: int
    budget: BudgetView
    steps: tuple[StepView, ...]
    decisions: tuple[DecisionView, ...]
    content: ContentView | None = None


class WorkingMemoryView(Strict):
    schema_version: str
    agent: str
    # idle: nothing tracked. A run list is empty only when no request has been observed since
    # this web process started; history before that is unavailable, not empty.
    state: Literal["idle", "active", "completed"]
    content_included: bool
    storage: str
    runs: tuple[WorkingMemoryRun, ...]



def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def project_run(state: OrchestrationState, *, content: bool, clock=time.monotonic) -> WorkingMemoryRun:
    """Project one state. Reads copies of the live lists, so a concurrent append cannot break it."""
    steps = list(state.completed_steps)[:MAX_STEPS]
    decisions = list(state.decision_history)[-MAX_DECISION_HISTORY:]
    pending = list(state.pending_gaps)
    reviews = list(state.reviews)
    budget = state.execution_budget
    try:
        current = fresh_results(state)
    except Exception:  # a partial state mid-update: report nothing as current rather than guess
        current = {}

    lifecycle: Literal["active", "completed", "released"]
    if state.status != "running":
        lifecycle = "completed"
    else:
        try:
            overdue = clock() - state.started_at > budget.max_seconds + RELEASE_GRACE_SECONDS
        except Exception:
            overdue = False
        lifecycle = "released" if overdue else "active"

    try:
        remaining = state.remaining(clock)
    except Exception:
        remaining = {"decisions": 0, "executions": 0, "seconds": 0.0}

    step_views = []
    for index, step in enumerate(steps, start=1):
        extra: dict = {}
        if content:
            observation = build_observation(step)
            extra = {"summary": observation.result_summary, "evidence_gaps": observation.evidence_gaps}
        step_views.append(StepView(
            sequence=index, identity=step.identity_id, capability=step.capability.value,
            service=step.service_name, version=step.version, decision_number=step.decision_number,
            duration_ms=step.duration_ms, based_on=dict(step.based_on),
            current=current.get(step.capability) is step, **extra))

    return WorkingMemoryRun(
        request_id=state.request_id,
        lifecycle=lifecycle,
        status=state.status,
        iteration=state.iteration,
        decision_count=state.decision_number,
        completion_kind=state.completion_kind,
        completion_reason=state.completion_reason,
        elapsed_ms=state.elapsed_ms,
        rejected_actions=state.rejected_actions,
        pending_gap_count=len(pending),
        peer_review_count=len(reviews),
        budget=BudgetView(
            max_decisions=budget.max_decisions, max_executions=budget.max_executions,
            max_repeats_per_capability=budget.max_repeats_per_capability,
            max_seconds=budget.max_seconds, max_rejected_actions=budget.max_rejected_actions,
            remaining_decisions=int(remaining["decisions"]),
            remaining_executions=int(remaining["executions"]),
            remaining_seconds=float(remaining["seconds"])),
        steps=tuple(step_views),
        decisions=tuple(DecisionView(
            decision_number=d.decision_number, requested_action=d.requested_action,
            capability=d.capability.value if d.capability else None, status=d.status,
            reason_code=d.reason_code, duration_ms=d.duration_ms) for d in decisions),
        content=ContentView(
            original_question=_clip(state.original_question, MAX_QUESTION_CHARS),
            current_goal=_clip(state.current_goal, MAX_QUESTION_CHARS),
            pending_gaps=tuple(_clip(g, 200) for g in pending)) if content else None,
    )


class WorkingMemoryRegistry:
    """In-process references to recent orchestration states. Nothing here is persisted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: OrderedDict[str, OrchestrationState] = OrderedDict()

    def track(self, state: OrchestrationState) -> None:
        """Remember a state. A no-op unless the explorer is enabled, so a disabled deployment
        retains no question text beyond the request itself."""
        if not explorer_enabled():
            return
        with self._lock:
            self._runs[state.request_id] = state
            self._runs.move_to_end(state.request_id)
            self._trim()

    def _trim(self) -> None:
        finished = [rid for rid, s in self._runs.items() if s.status != "running"]
        for rid in finished[: max(len(finished) - MAX_FINISHED, 0)]:
            del self._runs[rid]
        while len(self._runs) > MAX_ACTIVE + MAX_FINISHED:
            self._runs.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()

    def view(self, *, content: bool, clock=time.monotonic) -> WorkingMemoryView:
        with self._lock:
            states = list(self._runs.values())
        runs = tuple(project_run(s, content=content, clock=clock) for s in reversed(states))
        if not runs:
            state = "idle"
        elif any(r.lifecycle == "active" for r in runs):
            state = "active"
        else:
            state = "completed"
        return WorkingMemoryView(
            schema_version=SCHEMA, agent="charlie", state=state, content_included=content,
            storage="in-process references only; released on restart; never written to disk",
            runs=runs)


registry = WorkingMemoryRegistry()
