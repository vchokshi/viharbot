"""Canonical memory resource paths (ADR-010 path-addressed scopes).

The scope is always in the path and never selected by a query parameter or a body field. Parsing
works on the raw request path exactly as received, nothing is decoded, and any percent-encoding is
refused outright (no identifier here needs one), so the path the verifier signed, the path ADR-010
scopes match and the path this service acts on are the same bytes.

Shapes (one per tier, ``<scope>`` is an agent id or a session id)::

    /memory/agent/<agent_id>/records
    /memory/agent/<agent_id>/records/<memory_id>
    /memory/agent/<agent_id>/records/<memory_id>/<update|classify|retire|erase>
    /memory/session/<session_id>/records[...same]

``/memory/eng/...`` and ``/memory/inst/...`` are reserved and never served in this change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

AGENT_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MEMORY_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ACTIONS = frozenset({"update", "classify", "retire", "erase"})
SERVED_TIERS = frozenset({"agent", "session"})
RESERVED_TIERS = frozenset({"eng", "inst"})
MAX_PATH_BYTES = 256


class PathError(Exception):
    """The path is refused. ``reason`` is a fixed code that is safe to log, never to echo input."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReservedScope(PathError):
    """A tier that is reserved for later and is not served."""

    def __init__(self) -> None:
        super().__init__("reserved_scope")


@dataclass(frozen=True)
class MemoryPath:
    tier: str  # "agent" or "session"
    scope_key: str  # the agent id or the session id
    memory_id: str | None = None
    action: str | None = None  # None means the record itself (or the collection)

    @property
    def scope_label(self) -> str:
        return f"{self.tier}/{self.scope_key}"

    @property
    def memory_type(self) -> str:
        return "agent_private" if self.tier == "agent" else "session"

    def canonical(self) -> str:
        parts = ["/memory", self.tier, self.scope_key, "records"]
        if self.memory_id:
            parts.append(self.memory_id)
            if self.action:
                parts.append(self.action)
        return "/".join(parts)


def parse_memory_path(raw_path: bytes) -> MemoryPath:
    """Parse the raw path or raise PathError. Never decodes, never normalizes."""
    if not isinstance(raw_path, bytes) or not raw_path or len(raw_path) > MAX_PATH_BYTES:
        raise PathError("bad_length")
    if any(b < 0x21 or b > 0x7E for b in raw_path):
        raise PathError("not_printable_ascii")
    text = raw_path.decode("ascii")
    if "%" in text:
        raise PathError("percent_encoding_not_allowed")
    if "?" in text or "#" in text or ";" in text or "\\" in text:
        raise PathError("unexpected_delimiter")
    if not text.startswith("/memory/"):
        raise PathError("not_a_memory_path")
    segments = text.split("/")[1:]  # drop the empty piece before the leading slash
    if any(segment == "" for segment in segments):
        raise PathError("empty_segment")  # also rejects a trailing slash
    if any(segment in (".", "..") for segment in segments):
        raise PathError("dot_segment")
    tier = segments[1]
    if tier in RESERVED_TIERS:
        raise ReservedScope()
    if tier not in SERVED_TIERS:
        raise PathError("unknown_scope")
    if len(segments) < 4 or segments[3] != "records":
        raise PathError("unexpected_path")
    scope_key = segments[2]
    grammar = AGENT_ID if tier == "agent" else SESSION_ID
    if not grammar.fullmatch(scope_key):
        raise PathError("bad_scope_identifier")
    memory_id = action = None
    if len(segments) >= 5:
        memory_id = segments[4]
        if not MEMORY_ID.fullmatch(memory_id):
            raise PathError("bad_record_identifier")
    if len(segments) == 6:
        action = segments[5]
        if action not in ACTIONS:
            raise PathError("unknown_action")
    if len(segments) > 6:
        raise PathError("unexpected_path")
    parsed = MemoryPath(tier, scope_key, memory_id, action)
    if parsed.canonical() != text:  # belt and braces: the rebuilt path must equal the input
        raise PathError("not_canonical")
    return parsed
