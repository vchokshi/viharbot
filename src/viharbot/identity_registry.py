"""Load and validate the packaged agent identities.

Loading is deterministic and side-effect free: it reads files shipped with the package and
makes no network, LiteLLM, AWS, Docker or Strands calls.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from functools import cache
from pathlib import Path

from pydantic import ValidationError

from .identity import AgentIdentity

IDENTITY_DIR = Path(__file__).with_name("agent_identities")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class IdentityConfigError(ValueError):
    """An identity file, prompt or registry was invalid."""


class AgentRegistry:
    def __init__(self, identities: Iterable[AgentIdentity]) -> None:
        by_id: dict[str, AgentIdentity] = {}
        for identity in identities:
            if identity.id in by_id:
                raise IdentityConfigError(f"duplicate agent id: {identity.id}")
            by_id[identity.id] = identity
        self._by_id = by_id

    def get(self, agent_id: str) -> AgentIdentity:
        try:
            return self._by_id[agent_id]
        except KeyError:
            raise KeyError(f"unknown agent id: {agent_id}") from None

    def list(self) -> tuple[AgentIdentity, ...]:
        return tuple(self._by_id.values())

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)


def _describe(error: ValidationError) -> str:
    # Field paths and messages only; never echo the rejected values.
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'document'}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False)
    )


def _read_prompt(directory: Path, agent_id: str, source: str) -> str:
    prompts_dir = (directory / "prompts").resolve()
    path = (prompts_dir / f"{agent_id}.md").resolve()
    # The name comes from a validated id, so this only guards against symlink escapes.
    if path.parent != prompts_dir or not path.is_file():
        raise IdentityConfigError(f"{source}: prompt file prompts/{agent_id}.md is missing")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise IdentityConfigError(f"{source}: prompt file prompts/{agent_id}.md is empty")
    return text


def load_registry(directory: Path | None = None) -> AgentRegistry:
    """Load every `*.toml` identity in the packaged directory.

    `directory` exists for tests. It is never derived from request data.
    """
    root = directory or IDENTITY_DIR
    identities: list[AgentIdentity] = []
    for path in sorted(root.glob("*.toml")):
        source = path.name
        if not _ID_PATTERN.fullmatch(path.stem):
            raise IdentityConfigError(f"{source}: file name is not a valid agent id")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise IdentityConfigError(f"{source}: invalid TOML ({exc})") from None
        if data.get("id") != path.stem:
            raise IdentityConfigError(f"{source}: id must match the file name")
        if "system_prompt" in data:
            raise IdentityConfigError(f"{source}: system_prompt comes from prompts/{path.stem}.md")
        data["system_prompt"] = _read_prompt(root, path.stem, source)
        try:
            identities.append(AgentIdentity.model_validate(data))
        except ValidationError as exc:
            raise IdentityConfigError(f"{source}: {_describe(exc)}") from None
    if not identities:
        raise IdentityConfigError("no agent identities found")
    return AgentRegistry(identities)


@cache
def default_registry() -> AgentRegistry:
    return load_registry()
