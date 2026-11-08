"""Durable, correlated, tamper-evident audit records for Archie's workflows (issue #145).

One append-only JSON-lines file. Each record carries a sequence number and the hash of the previous
record, so deleting or editing a line is detectable by `verify()`. Records correlate by assignment id,
workflow id, artifact id and source revision.

What is never written: tokens, instructor text, answer keys, prompts, model output or personal data
beyond a GitHub login. `detail` is a small dictionary with allowlisted key names and short scalar
values; a value that looks like a credential is replaced, not stored. Instructor reads are recorded
as a path hash plus an outcome, the same shape `instructor_access` already logs.

Every append also writes one structured JSON log line through the existing `archie-learning` logger
(identifiers and outcome only), so the standard observability pipeline sees the same facts. A caller
may pass `sink` to forward records to another consumer (for example the live-event layer later).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .fsutil import locked

logger = logging.getLogger("archie-learning")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SECRETISH = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{12,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer\s+[A-Za-z0-9._~+/=-]{16,}|sk-[A-Za-z0-9]{20,})")
MAX_DETAIL_KEYS = 8
MAX_VALUE_CHARS = 120
GENESIS = "0" * 64


class AuditEvent(StrEnum):
    ASSIGNMENT_CREATED = "assignment.created"
    ASSIGNMENT_CANCELLED = "assignment.cancelled"
    ASSIGNMENT_COMPLETED = "assignment.completed"
    ASSIGNMENT_REFUSED = "assignment.refused"
    EVIDENCE_RETRIEVED = "evidence.retrieved"
    EVIDENCE_SAGE = "evidence.sage_reference"
    EVIDENCE_KEPLER = "evidence.kepler_reference"
    INSTRUCTOR_ACCESS = "instructor.access_decision"
    DRAFT_CREATED = "draft.created"
    DRAFT_REVISED = "draft.revised"
    MODEL_DRAFT_REJECTED = "draft.model_rejected"
    REVIEW_SUBMITTED = "review.submitted"
    REVIEW_CHANGES_REQUESTED = "review.changes_requested"
    APPROVED = "review.approved"
    APPROVAL_INVALIDATED = "review.approval_invalidated"
    REJECTED = "transition.refused"
    PUBLISHED = "publication.published"
    REVOKED = "publication.revoked"
    ADA_RETRIEVED = "ada.package_retrieved"
    ADA_REFUSED = "ada.retrieval_refused"
    FEEDBACK_PREPARED = "feedback.prepared"
    FEEDBACK_REFUSED = "feedback.refused"
    FEEDBACK_SUBMITTED = "feedback.submitted"
    FEEDBACK_APPROVED = "feedback.approved"
    FEEDBACK_RELEASED = "feedback.released"
    FEEDBACK_STALE = "feedback.stale"
    FEEDBACK_REVOKED = "feedback.revoked"
    FEEDBACK_RETRIEVED = "feedback.retrieved"
    WORKFLOW_FAILED = "workflow.failed"


Scalar = str | int | bool


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    seq: int = Field(ge=1)
    at: str
    event: AuditEvent
    outcome: Literal["ok", "refused", "error"]
    actor: str
    assignment_id: str | None = None
    workflow_id: str | None = None
    artifact_id: str | None = None
    source_revision: str | None = None
    key: str | None = None  # dedupe key: an append with a key already present is a no-op
    detail: dict[str, Scalar] = Field(default_factory=dict)
    prev_hash: str
    hash: str


def _clean(detail: dict[str, Scalar] | None) -> dict[str, Scalar]:
    out: dict[str, Scalar] = {}
    for key, value in (detail or {}).items():
        if not _KEY.match(key) or len(out) >= MAX_DETAIL_KEYS:
            continue  # dropped silently: an unlisted shape is never stored
        if isinstance(value, str):
            value = "[redacted]" if _SECRETISH.search(value) else value[:MAX_VALUE_CHARS]
        out[key] = value
    return out


def _digest(prev: str, body: dict) -> str:
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()


class AuditLog:
    def __init__(self, directory: Path, sink: Callable[[AuditRecord], None] | None = None) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = directory / "audit.jsonl"
        self._lock = directory / ".audit.lock"
        self._sink = sink

    def _read(self) -> list[AuditRecord]:
        if not self._file.is_file():
            return []
        return [AuditRecord.model_validate_json(line)
                for line in self._file.read_text(encoding="utf-8").splitlines() if line.strip()]

    def append(self, event: AuditEvent, actor: str, outcome: Literal["ok", "refused", "error"] = "ok",
               *, assignment_id: str | None = None, workflow_id: str | None = None,
               artifact_id: str | None = None, source_revision: str | None = None,
               key: str | None = None, detail: dict[str, Scalar] | None = None) -> AuditRecord:
        with locked(self._lock):
            rows = self._read()
            if key is not None:
                for row in rows:
                    if row.key == key:
                        return row  # a retry of an operation already recorded
            prev = rows[-1].hash if rows else GENESIS
            body = {"seq": len(rows) + 1, "at": datetime.now(timezone.utc).isoformat(),
                    "event": event.value, "outcome": outcome, "actor": actor[:80],
                    "assignment_id": assignment_id, "workflow_id": workflow_id,
                    "artifact_id": artifact_id, "source_revision": source_revision, "key": key,
                    "detail": _clean(detail)}
            record = AuditRecord(**body, prev_hash=prev, hash=_digest(prev, body))
            with open(self._file, "a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")
                handle.flush()
        logger.info("archie_audit", extra={
            "agent": "archie", "request_id": workflow_id or assignment_id or "-",
            "destination_agent": "audit", "model_alias": None, "decision": outcome,
            "reason": event.value, "target": artifact_id or assignment_id})
        if self._sink is not None:
            self._sink(record)
        return record

    def records(self, **match: str) -> list[AuditRecord]:
        """All records, optionally filtered by exact field values (assignment_id=..., event=...)."""
        out = self._read()
        for field_name, value in match.items():
            out = [r for r in out if str(getattr(r, field_name, None)) == str(value)
                   or (field_name == "event" and r.event.value == str(value))]
        return out

    def verify(self) -> bool:
        """True if the chain is intact: sequence, links and hashes all check out."""
        prev = GENESIS
        for index, record in enumerate(self._read(), start=1):
            body = record.model_dump(mode="json", exclude={"prev_hash", "hash"})
            if (record.seq != index or record.prev_hash != prev or record.hash != _digest(prev, body)):
                return False
            prev = record.hash
        return True
