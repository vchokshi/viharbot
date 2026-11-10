"""The memory policy table: which verified caller may do what (deny by default).

Reviewed repository configuration (``execution_config/memory_policy.toml``), never inferred from a
token claim, a body, a prompt or the registry alone. Being named here is necessary and not
sufficient: the caller must also present a valid A2 assertion whose registry entry allows the route
capability. The ``sub`` of the verified assertion is the only input; the agent identity is the
``agent_id`` this table maps that ``sub`` to.

A missing, unreadable or invalid table at start refuses to start the service. If a later reload
fails, the service falls back to an empty table, which denies everything (fail closed).
"""

from __future__ import annotations

import logging
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .paths import AGENT_ID

logger = logging.getLogger("memory_policy")

DEFAULT_POLICY_FILE = Path(__file__).resolve().parent.parent / "execution_config" / "memory_policy.toml"

OPERATIONS = ("read", "create", "update", "classify", "retire", "erase")
# Lowest to highest. A writer may raise a record's class up to its ceiling and never lower it.
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
HARD_MAX_CONTENT_BYTES = 65_536  # the database also enforces this

_AGENT_KEYS = frozenset({
    "agent_id", "subs", "agent_private", "session", "classification_ceiling", "allow_personal_data",
    "default_retention_days", "max_retention_days", "session_default_ttl_seconds",
    "session_max_ttl_seconds", "max_content_bytes",
})


class PolicyConfigError(ValueError):
    """The policy table is malformed. Never half-applied."""


class Denied(Exception):
    """A request is refused by policy. ``reason`` is a fixed code for the audit log only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def classification_rank(name: str) -> int:
    return CLASSIFICATIONS.index(name)


def classes_up_to(ceiling: str) -> list[str]:
    return list(CLASSIFICATIONS[: classification_rank(ceiling) + 1])


@dataclass(frozen=True)
class AgentPolicy:
    agent_id: str
    subs: frozenset[str]
    agent_private: frozenset[str]
    session: frozenset[str]
    classification_ceiling: str
    allow_personal_data: bool
    default_retention_days: int
    max_retention_days: int
    session_default_ttl_seconds: int
    session_max_ttl_seconds: int
    max_content_bytes: int


class MemoryPolicy:
    def __init__(self, agents: list[AgentPolicy]) -> None:
        self._by_sub: dict[str, AgentPolicy] = {}
        for agent in agents:
            for sub in agent.subs:
                if sub in self._by_sub:
                    raise PolicyConfigError("a sub may map to only one agent")
                self._by_sub[sub] = agent
        self.agents = tuple(agents)

    @classmethod
    def empty(cls) -> "MemoryPolicy":
        return cls([])

    def for_sub(self, sub: str) -> AgentPolicy | None:
        return self._by_sub.get(sub)

    def authorize(self, sub: str, tier: str, scope_key: str, operation: str) -> AgentPolicy:
        """Return the caller's agent policy or raise Denied. The scope must be the caller's own."""
        agent = self._by_sub.get(sub)
        if agent is None:
            raise Denied("sub_not_in_policy")
        if tier == "agent":
            if scope_key != agent.agent_id:
                raise Denied("scope_not_owned")
            allowed = agent.agent_private
        elif tier == "session":
            allowed = agent.session  # a session id only partitions the caller's own records
        else:
            raise Denied("tier_not_served")
        if operation not in allowed:
            raise Denied("operation_not_permitted")
        return agent


def _ints(table: dict, key: str, low: int, high: int) -> int:
    value = table.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise PolicyConfigError(f"{key} must be an integer from {low} to {high}")
    return value


def _operations(table: dict, key: str) -> frozenset[str]:
    value = table.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyConfigError(f"{key} must be a list of operation names")
    unknown = set(value) - set(OPERATIONS)
    if unknown or len(set(value)) != len(value):
        raise PolicyConfigError(f"{key} has an unknown or repeated operation")
    return frozenset(value)


def parse_policy(text: str) -> MemoryPolicy:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError):
        raise PolicyConfigError("policy is not valid TOML") from None
    if set(data) - {"version", "agent"} or data.get("version") != 1:
        raise PolicyConfigError("policy needs version = 1 and [[agent]] tables only")
    items = data.get("agent", [])
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise PolicyConfigError("agent must be a list of tables")
    agents: list[AgentPolicy] = []
    seen: set[str] = set()
    for item in items:
        if set(item) - _AGENT_KEYS or _AGENT_KEYS - set(item):
            raise PolicyConfigError("an agent table has a missing or unknown key")
        agent_id = item["agent_id"]
        if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id) or agent_id in seen:
            raise PolicyConfigError("agent_id must be a unique valid agent id")
        seen.add(agent_id)
        subs = item["subs"]
        if not isinstance(subs, list) or not subs or not all(
            isinstance(s, str) and s and s.isascii() and s.isprintable() for s in subs
        ):
            raise PolicyConfigError("subs must be a non-empty list of strings")
        ceiling = item["classification_ceiling"]
        if ceiling not in CLASSIFICATIONS:
            raise PolicyConfigError("classification_ceiling is not a known class")
        if not isinstance(item["allow_personal_data"], bool):
            raise PolicyConfigError("allow_personal_data must be a boolean")
        default_days = _ints(item, "default_retention_days", 1, 3650)
        max_days = _ints(item, "max_retention_days", 1, 3650)
        default_ttl = _ints(item, "session_default_ttl_seconds", 1, 30 * 86400)
        max_ttl = _ints(item, "session_max_ttl_seconds", 1, 30 * 86400)
        if default_days > max_days or default_ttl > max_ttl:
            raise PolicyConfigError("a default may not exceed its maximum")
        agents.append(AgentPolicy(
            agent_id=agent_id,
            subs=frozenset(subs),
            agent_private=_operations(item, "agent_private"),
            session=_operations(item, "session"),
            classification_ceiling=ceiling,
            allow_personal_data=item["allow_personal_data"],
            default_retention_days=default_days,
            max_retention_days=max_days,
            session_default_ttl_seconds=default_ttl,
            session_max_ttl_seconds=max_ttl,
            max_content_bytes=_ints(item, "max_content_bytes", 1, HARD_MAX_CONTENT_BYTES),
        ))
    return MemoryPolicy(agents)


def load_policy(path: str | Path = DEFAULT_POLICY_FILE) -> MemoryPolicy:
    try:
        return parse_policy(Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise PolicyConfigError("policy file is unreadable") from None


class PolicyProvider:
    """Hands out the current policy and reloads the file when it changes, failing closed.

    The policy is never cached across a decision that matters: the service reads it once to
    authorize and asks again inside the write transaction (see the guard in the repository), so a
    revocation that lands between the two refuses the write.
    """

    def __init__(self, policy: MemoryPolicy, path: Path | None = None) -> None:
        self._policy = policy
        self._path = path
        self._stamp = self._stat()
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: str | Path = DEFAULT_POLICY_FILE) -> "PolicyProvider":
        return cls(load_policy(path), Path(path))  # raises on a bad file: the service must not start

    def _stat(self) -> tuple[int, int] | None:
        try:
            stat = self._path.stat() if self._path else None
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size) if stat else None

    def replace(self, policy: MemoryPolicy) -> None:
        with self._lock:
            self._policy = policy

    def current(self) -> MemoryPolicy:
        if self._path is None:
            return self._policy
        with self._lock:
            stamp = self._stat()
            if stamp != self._stamp:
                self._stamp = stamp
                try:
                    self._policy = load_policy(self._path)
                except PolicyConfigError as exc:
                    logger.warning(
                        "memory policy reload failed; denying everything",
                        extra={"agent": "memory", "reason": str(exc)},
                    )
                    self._policy = MemoryPolicy.empty()
            return self._policy
