"""Deterministic planning of identity-attributed workflows.

The planner uses fixed rules only: no network, no LLM, no Strands agents, no commands.
"""

from __future__ import annotations

from collections.abc import Collection

from pydantic import BaseModel, ConfigDict

from .execution_bindings import ExecutionBindingSet
from .identity import AgentId, Capability, NonEmpty
from .identity_registry import AgentRegistry

# Fixed, repository-defined workflows. Each lists capabilities in execution order.
WORKFLOWS: dict[str, tuple[Capability, ...]] = {
    "full_review": (
        Capability.RESEARCH,
        Capability.FACT_CHECK,
        Capability.SOURCE_QUALITY,
        Capability.SECURITY_ASSESSMENT,
        Capability.PRIVACY_REVIEW,
        Capability.SYNTHESIS,
    ),
    "research_security": (Capability.RESEARCH, Capability.SECURITY_ASSESSMENT),
}

# The order in which the existing pipeline can run services, given their data dependencies.
PIPELINE_ORDER: tuple[str, ...] = (
    "research",
    "fact-check",
    "source-quality",
    "security",
    "privacy-review",
    "synthesis",
)


class PlanningError(ValueError):
    """A workflow could not be planned. Messages never include user-supplied values."""


class ExecutionStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    identity_id: AgentId
    capability: Capability
    service_name: NonEmpty


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    workflow_name: str
    steps: tuple[ExecutionStep, ...]

    @property
    def services(self) -> tuple[str, ...]:
        return tuple(step.service_name for step in self.steps)


def validate_dispatchable(plan: ExecutionPlan) -> None:
    """Check the plan matches what the existing pipeline can execute, in its fixed order."""
    services = plan.services
    positions = [PIPELINE_ORDER.index(s) for s in services if s in PIPELINE_ORDER]
    if len(positions) != len(services) or positions != sorted(set(positions)):
        raise PlanningError("plan order does not match the pipeline's dependency order")
    if services[:1] != ("research",) or "security" not in services:
        raise PlanningError("plan must include research first and security")


def plan_workflow(
    workflow_name: str,
    bindings: ExecutionBindingSet,
    registry: AgentRegistry,
    available_services: Collection[str],
) -> ExecutionPlan:
    capabilities = WORKFLOWS.get(workflow_name) if isinstance(workflow_name, str) else None
    if capabilities is None:
        raise PlanningError("unsupported workflow")

    steps: list[ExecutionStep] = []
    for capability in capabilities:
        binding = bindings.lookup(capability)
        if binding is None:
            raise PlanningError(f"no execution binding for capability {capability.value}")
        if not binding.enabled:
            raise PlanningError(f"execution binding for {capability.value} is disabled")
        if binding.identity_id not in registry:
            raise PlanningError(f"binding identity for {capability.value} is not registered")
        if binding.service_name not in available_services:
            raise PlanningError(f"service for {capability.value} is not configured")
        steps.append(
            ExecutionStep(
                identity_id=binding.identity_id,
                capability=capability,
                service_name=binding.service_name,
            )
        )
    plan = ExecutionPlan(workflow_name=workflow_name, steps=tuple(steps))
    validate_dispatchable(plan)
    return plan
