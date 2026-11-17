"""Structured audit of every memory read, write and denial (ADR-009: identifiers, never content).

One JSON line per decision through the shared formatter: the service, the request ID, the verified
caller, the scope, the operation and the outcome, plus a fixed reason code and the record id where
there is one. A successful list also carries ``count``, how many records it returned. The
arguments are all identifiers, fixed codes or that number; there is deliberately no parameter that
could carry content, a token, a key or a request body.
"""

from __future__ import annotations

import logging

audit_logger = logging.getLogger("memory_audit")

OK_OUTCOMES = frozenset({"ok", "replayed"})


def audit(
    *,
    request_id: str,
    caller: str | None,
    scope: str,
    operation: str,
    outcome: str,
    reason: str | None = None,
    memory_id: str | None = None,
    count: int | None = None,
) -> None:
    log = audit_logger.info if outcome in OK_OUTCOMES else audit_logger.warning
    extra: dict[str, object] = {
        "agent": "memory",
        "request_id": request_id,
        "caller": caller,
        "scope": scope,
        "operation": operation,
        "outcome": outcome,
    }
    if reason:
        extra["reason"] = reason
    if memory_id:
        extra["memory_id"] = memory_id
    if count is not None and operation == "list" and outcome in OK_OUTCOMES:
        extra["count"] = count
    log("memory audit", extra=extra)
