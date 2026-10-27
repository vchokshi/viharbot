"""The sanitized, public view of an iterative orchestration.

Built only from the real orchestration state. It carries operational facts (which approved
capability ran, in what order, how long it took, how the run ended) and nothing the model or
the user wrote: no rationales, unresolved questions, observations, prompts, model aliases,
URLs or credentials. Enumerated fields are Literal types so they cannot carry free text.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .iterative_execution import IterativePipelineResult
from .orchestration_policy import fresh_results

CompletionReason = Literal[
    "charlie_finished",
    "decision_budget",
    "execution_budget",
    "time_budget",
    "rejection_limit",
]


class OrchestrationStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int  # 1-based execution order
    identity: str  # approved identity id, e.g. "nova"
    capability: str
    service: str
    version: int  # 1 is the first run of this capability; 2 and up are repeats
    status: Literal["complete"]
    duration_ms: float
    # False when newer evidence superseded this result and it was left out of the response.
    in_final_result: bool


class OrchestrationMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["iterative"]  # set explicitly: the route omits unset fields
    status: Literal["completed", "budget_exhausted"]
    completion_kind: Literal["charlie_approved", "policy_budget"]
    completion_reason: CompletionReason
    decisions: int  # planning decisions Charlie was asked for
    executions: int  # delegated executions that completed
    rejected_actions: int  # actions the application refused
    elapsed_ms: float
    steps: list[OrchestrationStep]


def build_orchestration_meta(result: IterativePipelineResult) -> OrchestrationMeta:
    """Describe what actually happened. Only completed runs reach this point."""
    state = result.orchestration
    in_result = fresh_results(state)
    return OrchestrationMeta(
        mode="iterative",
        status=state.status,
        completion_kind=state.completion_kind,
        completion_reason=state.completion_reason,
        decisions=state.decision_number,
        executions=len(state.completed_steps),
        rejected_actions=state.rejected_actions,
        elapsed_ms=state.elapsed_ms or 0.0,
        steps=[
            OrchestrationStep(
                sequence=index,
                identity=step.identity_id,
                capability=step.capability.value,
                service=step.service_name,
                version=step.version,
                status="complete",
                duration_ms=step.duration_ms,
                in_final_result=in_result.get(step.capability) is step,
            )
            for index, step in enumerate(state.completed_steps, start=1)
        ],
    )
