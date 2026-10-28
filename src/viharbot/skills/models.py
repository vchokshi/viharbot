"""Typed skill definition.

A skill says what professional work an agent could do. Like a capability, it describes
suitability and never grants authorization: a skill declaration confers no tool, scope, data or
infrastructure access, and a skill that is not `enabled` cannot execute.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ..identity import AgentId, Capability, NonEmpty

SkillId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")]
SemVer = Annotated[str, StringConstraints(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")]
# "skill:<skill_id>" or "capability:<capability>"
Dependency = Annotated[
    str, StringConstraints(pattern=r"^(skill:[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*|capability:[a-z_]+)$")
]
Text = tuple[NonEmpty, ...]


class SkillStatus(StrEnum):
    """Lifecycle: proposed, defined, implemented, tested, enabled, deprecated."""

    PROPOSED = "proposed"  # concept identified; no execution contract guaranteed
    DEFINED = "defined"  # metadata and input/output contracts approved
    IMPLEMENTED = "implemented"  # an executable implementation exists
    TESTED = "tested"  # required automated tests pass
    ENABLED = "enabled"  # approved for execution in a specified environment
    DEPRECATED = "deprecated"  # no longer available for new assignments


# Statuses that require an input/output contract and evaluation criteria.
CONTRACTED = frozenset(
    {SkillStatus.DEFINED, SkillStatus.IMPLEMENTED, SkillStatus.TESTED, SkillStatus.ENABLED}
)
# Statuses that require an executable implementation, hence an existing capability.
EXECUTABLE_IMPLEMENTATION = frozenset(
    {SkillStatus.IMPLEMENTED, SkillStatus.TESTED, SkillStatus.ENABLED}
)


class SkillDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: SkillId
    version: SemVer
    name: NonEmpty
    description: NonEmpty
    owner: AgentId
    eligible_agents: Text = ()
    category: NonEmpty
    inputs: Text = ()
    outputs: Text = ()
    required_tools: Text = ()
    required_scopes: Text = ()
    data_classifications: Text = ()
    dependencies: Annotated[tuple[Dependency, ...], Field()] = ()
    evaluation: Text = ()
    status: SkillStatus
    # Informational link to an existing capability this skill corresponds to. It does not
    # bind the skill to a service; only execution bindings do that.
    capability: Capability | None = None

    @property
    def is_executable(self) -> bool:
        """Only an `enabled` skill may run, and running still needs an approved binding."""
        return self.status is SkillStatus.ENABLED

    @model_validator(mode="after")
    def _consistent(self) -> SkillDefinition:
        domain = self.skill_id.split(".", 1)[0]
        if self.category != domain:
            raise ValueError("category must match the skill_id domain")
        if self.owner in self.eligible_agents:
            raise ValueError("eligible_agents must not repeat the owner")
        for field in ("eligible_agents", "dependencies"):
            values = getattr(self, field)
            if len(set(values)) != len(values):
                raise ValueError(f"{field} must not repeat")
        if f"skill:{self.skill_id}" in self.dependencies:
            raise ValueError("a skill must not depend on itself")
        if self.status in CONTRACTED and not (self.inputs and self.outputs and self.evaluation):
            raise ValueError(f"status {self.status.value} requires inputs, outputs and evaluation")
        if self.status in EXECUTABLE_IMPLEMENTATION and self.capability is None:
            raise ValueError(f"status {self.status.value} requires an existing capability")
        return self

