"""Structured, hash-chained audit events.

Only named fields are ever written: never tokens, environment values, prompts or file contents.
Each event carries the SHA-256 of its predecessor, so removing, reordering or editing a stored
event is detectable by `verify_chain`. The chain proves integrity of the record, not who wrote it;
it is not a substitute for protecting the store.
"""

from __future__ import annotations

import hashlib
import json
import logging

FIELDS = (
    "request_id", "run_id", "issue", "actor", "state_from", "state_to", "branch", "pr_number",
    "reason", "error", "duration_ms", "exit_code", "command", "output_sha256", "commit",
    "governance",
)
GENESIS = "0" * 64
logger = logging.getLogger("kepler")


class AuditIntegrityError(RuntimeError):
    """The stored audit chain has been altered."""


def _digest(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_event(
    previous: dict | None, *, timestamp: str, event: str, decision: str, fields: dict
) -> dict:
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown audit fields: {sorted(unknown)}")
    body = {
        "seq": 0 if previous is None else previous["seq"] + 1,
        "timestamp": timestamp,
        "agent": "kepler",
        "event": event,
        "decision": decision,
        "prev_sha256": GENESIS if previous is None else previous["sha256"],
        **{name: fields.get(name) for name in FIELDS},
    }
    return {**body, "sha256": _digest(body)}


def verify_chain(events: list[dict]) -> None:
    previous = GENESIS
    for index, event in enumerate(events):
        body = {k: v for k, v in event.items() if k != "sha256"}
        if (
            event.get("seq") != index
            or event.get("prev_sha256") != previous
            or event.get("sha256") != _digest(body)
        ):
            raise AuditIntegrityError(f"audit chain broken at event {index}")
        previous = event["sha256"]


def log_event(event: dict) -> None:
    log = logger.warning if event["decision"] in ("deny", "fail") else logger.info
    log(
        json.dumps(event, sort_keys=True),
        extra={
            "agent": "kepler", "request_id": event.get("request_id"),
            "decision": event["decision"], "reason": event.get("reason"),
            "caller": event.get("actor"),
        },
    )
