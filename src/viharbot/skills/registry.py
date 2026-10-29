"""Load and validate the declarative skills registry.

Loading is deterministic and side-effect free: it reads files shipped with the package and makes
no network, LiteLLM, AWS, Docker or Strands calls. Nothing in the application runtime reads this
registry yet, so declaring a skill cannot change execution.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from functools import cache
from pathlib import Path

from pydantic import ValidationError

from ..identity import CAPABILITY_STATUS, Capability, CapabilityStatus
from ..identity_registry import AgentRegistry, default_registry
from .models import EXECUTABLE_IMPLEMENTATION, SkillDefinition, SkillStatus

SKILLS_DIR = Path(__file__).with_name("definitions")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class SkillConfigError(ValueError):
    """A skill definition file or the skills registry was invalid."""


class SkillRegistry:
    """Skills validated against the identity registry, ordered by skill id."""

    def __init__(self, skills: Iterable[SkillDefinition], agents: AgentRegistry) -> None:
        by_id: dict[str, SkillDefinition] = {}
        for skill in skills:
            if skill.skill_id in by_id:
                raise SkillConfigError(f"{skill.skill_id}: duplicate skill id")
            by_id[skill.skill_id] = skill
        for skill in by_id.values():
            self._check(skill, by_id, agents)
        self._check_cycles(by_id)
        self._by_id = dict(sorted(by_id.items()))

    @staticmethod
    def _check(
        skill: SkillDefinition, by_id: dict[str, SkillDefinition], agents: AgentRegistry
    ) -> None:
        where = skill.skill_id
        if skill.owner not in agents:
            raise SkillConfigError(f"{where}: unknown owner")
        for agent in skill.eligible_agents:
            if agent not in agents:
                raise SkillConfigError(f"{where}: unknown eligible agent")
        if skill.capability is not None:
            if skill.capability not in agents.get(skill.owner).capabilities:
                raise SkillConfigError(f"{where}: owner does not declare this capability")
            if (
                skill.status in EXECUTABLE_IMPLEMENTATION
                and CAPABILITY_STATUS[skill.capability] is not CapabilityStatus.IMPLEMENTED
            ):
                raise SkillConfigError(f"{where}: capability is planned and cannot execute")
        for dependency in skill.dependencies:
            kind, _, name = dependency.partition(":")
            if kind == "capability":
                if name not in {c.value for c in Capability}:
                    raise SkillConfigError(f"{where}: unknown capability dependency")
                continue
            target = by_id.get(name)
            if target is None:
                raise SkillConfigError(f"{where}: unknown skill dependency")
            if target.status is SkillStatus.DEPRECATED and skill.status is not SkillStatus.DEPRECATED:
                raise SkillConfigError(f"{where}: depends on a deprecated skill")
            if skill.status is not SkillStatus.PROPOSED and target.status is SkillStatus.PROPOSED:
                raise SkillConfigError(f"{where}: depends on a skill that is only proposed")

    @staticmethod
    def _check_cycles(by_id: dict[str, SkillDefinition]) -> None:
        done: set[str] = set()

        def visit(skill_id: str, path: tuple[str, ...]) -> None:
            if skill_id in path:
                raise SkillConfigError(f"{skill_id}: circular dependency")
            if skill_id in done:
                return
            for dependency in by_id[skill_id].dependencies:
                if dependency.startswith("skill:"):
                    visit(dependency.removeprefix("skill:"), (*path, skill_id))
            done.add(skill_id)

        for skill_id in sorted(by_id):
            visit(skill_id, ())

    def get(self, skill_id: str) -> SkillDefinition:
        try:
            return self._by_id[skill_id]
        except KeyError:
            raise KeyError(f"unknown skill id: {skill_id}") from None

    def list(self) -> tuple[SkillDefinition, ...]:
        return tuple(self._by_id.values())

    def owned_by(self, agent_id: str) -> tuple[SkillDefinition, ...]:
        return tuple(s for s in self._by_id.values() if s.owner == agent_id)

    def executable(self) -> tuple[SkillDefinition, ...]:
        """Skills whose lifecycle permits execution. Empty until a skill is approved."""
        return tuple(s for s in self._by_id.values() if s.is_executable)

    def __contains__(self, skill_id: object) -> bool:
        return skill_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)


def _describe(error: ValidationError) -> str:
    # Field paths and messages only; never echo the rejected values.
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'skill'}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False)
    )


def load_skill_registry(
    directory: Path | None = None, agents: AgentRegistry | None = None
) -> SkillRegistry:
    """Load every `*.toml` skill file in the packaged directory.

    `directory` exists for tests. It is never derived from request data.
    """
    root = directory or SKILLS_DIR
    skills: list[SkillDefinition] = []
    for path in sorted(root.glob("*.toml")):
        source = path.name
        if not _ID_PATTERN.fullmatch(path.stem):
            raise SkillConfigError(f"{source}: file name is not a valid agent id")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise SkillConfigError(f"{source}: invalid TOML ({exc})") from None
        if set(data) - {"skill"}:
            raise SkillConfigError(f"{source}: unexpected top-level keys")
        entries = data.get("skill", [])
        if not isinstance(entries, list) or not entries:
            raise SkillConfigError(f"{source}: no skills defined")
        for index, entry in enumerate(entries):
            try:
                skill = SkillDefinition.model_validate(entry)
            except ValidationError as exc:
                raise SkillConfigError(f"{source} skill[{index}]: {_describe(exc)}") from None
            if skill.owner != path.stem:
                raise SkillConfigError(f"{source} skill[{index}]: owner must match the file name")
            skills.append(skill)
    if not skills:
        raise SkillConfigError("no skill definitions found")
    return SkillRegistry(skills, agents or default_registry())


@cache
def default_skill_registry() -> SkillRegistry:
    return load_skill_registry()
