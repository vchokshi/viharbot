"""Typed workflow recommendations and the deterministic policy that authorizes them.

A DelegationDecision is untrusted model output. It names one predefined workflow and nothing
else. Only validate_delegation() can turn it into an executable plan, and that goes through
the same planner, bindings and dispatch rules as every other workflow.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .execution_bindings import ExecutionBindingSet
from .execution_planner import ExecutionPlan, plan_workflow
from .identity import Capability
from .identity_registry import AgentRegistry

# Workflows a model may recommend. Deliberately explicit: a workflow added to the planner
# later is not delegable until it is added here.
DELEGABLE_WORKFLOWS: frozenset[str] = frozenset({"full_review", "research_security"})


class DelegationDecision(BaseModel):
    """A recommendation, not an executable plan. It has no field for services, URLs,
    runtime identifiers, model aliases, tools, credentials or execution steps."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    workflow_name: Literal["full_review", "research_security"]
    rationale: str = Field(min_length=1, max_length=500)
    confidence: Literal["low", "medium", "high"]


class DelegationError(RuntimeError):
    """Base class. Messages are fixed strings; they never carry prompts, aliases or user text."""

    def __init__(self, message: str, cause_type: str | None = None) -> None:
        super().__init__(message)
        self.cause_type = cause_type


class PlannerUnavailableError(DelegationError):
    """Model-assisted planning is not configured."""


class PlannerInferenceError(DelegationError):
    """The planning call failed or timed out."""


class PlannerOutputError(DelegationError):
    """The planner did not return a valid DelegationDecision."""


class DelegationRejected(DelegationError):
    """Policy refused the recommendation. Nothing was executed."""


def validate_delegation(
    decision: DelegationDecision,
    registry: AgentRegistry,
    bindings: ExecutionBindingSet,
    available_services: Collection[str],
) -> ExecutionPlan:
    """Authorize a recommendation. No model calls, no network, no agent invocation.

    Model confidence and rationale play no part in the decision.
    """
    if not isinstance(decision, DelegationDecision):
        raise DelegationRejected("recommendation is not a valid decision")
    if decision.workflow_name not in DELEGABLE_WORKFLOWS:
        raise DelegationRejected("workflow is not approved for delegation")
    # plan_workflow enforces enabled bindings, implemented capabilities, configured
    # services and the pipeline's dependency order.
    plan = plan_workflow(decision.workflow_name, bindings, registry, available_services)
    if Capability.SECURITY_ASSESSMENT not in {step.capability for step in plan.steps}:
        raise DelegationRejected("the security assessment is mandatory")
    return plan
