"""Founder assignments: the only way Archie's curriculum work can start (epic #139, issue #140).

An assignment names a topic and the exact source pull requests Archie may study. Only a login on the
configured founder list can create or cancel one; with no founder list configured, nothing can be
assigned (fail closed). The ingestion pipeline refuses any pull request that no active assignment
covers, so a commit, a merge, a Sage review or a scheduled `reconcile` cannot start a lesson.

Two ways to name the founder. A `HumanActor` from `authn.CredentialAuthenticator` proves a token and
is the only form an API or service may use: build the store with `require_authenticated=True` and a
bare login string is refused. The legacy bare-login form remains for the local CLI when no token
hashes are configured; there the identity is asserted by whoever runs it, not proven, and the control
is access to the store directory. No webhook, event handler or agent path imports `assign`.

Scope names the repository, the exact pull requests and the evidence kinds Archie may use
(`permitted_evidence`); instructor guidance and Kepler handoffs are off unless the founder grants
them, and instructor reads are further limited to `instructor_paths`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .audit import AuditEvent, AuditLog
from .authn import Actor, HumanActor, Role
from .fsutil import atomic_write, locked
from .models import Repo

MAX_PULL_REQUESTS = 20
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class NotFounder(PermissionError):
    """The caller is not on the founder list."""


class NotAssigned(PermissionError):
    """No active founder assignment covers this source revision."""


class EvidenceScope(StrEnum):
    SOURCE_REVISION = "source_revision"
    SAGE_REVIEW = "sage_review"
    GOVERNANCE = "governance"
    KEPLER_HANDOFF = "kepler_handoff"          # off by default; format is issue #130
    INSTRUCTOR_GUIDANCE = "instructor_guidance"  # off by default; also needs instructor_paths


DEFAULT_EVIDENCE = (EvidenceScope.SOURCE_REVISION, EvidenceScope.SAGE_REVIEW, EvidenceScope.GOVERNANCE)
MAX_INSTRUCTOR_PATHS = 10


class AssignmentState(StrEnum):
    ASSIGNED = "assigned"      # work may run, within scope
    CANCELLED = "cancelled"    # founder withdrew it; no further work
    COMPLETED = "completed"    # founder closed it out; no further work


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignment_id: str
    topic: str = Field(min_length=1, max_length=200)
    source_repository: Repo
    pull_requests: tuple[int, ...] = Field(min_length=1, max_length=MAX_PULL_REQUESTS)
    assigned_by: str
    assigned_at: str
    state: AssignmentState = AssignmentState.ASSIGNED
    closed_by: str | None = None
    closed_at: str | None = None
    permitted_evidence: tuple[EvidenceScope, ...] = DEFAULT_EVIDENCE
    instructor_paths: tuple[str, ...] = Field(default=(), max_length=MAX_INSTRUCTOR_PATHS)
    idempotency_key: str | None = None

    @field_validator("pull_requests")
    @classmethod
    def _positive_unique(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(n < 1 for n in value) or len(set(value)) != len(value):
            raise ValueError("pull requests must be unique positive numbers")
        return value

    def permits(self, scope: EvidenceScope) -> bool:
        return self.state is AssignmentState.ASSIGNED and scope in self.permitted_evidence

    def covers(self, repository: str, pull_request: int) -> bool:
        return (self.state is AssignmentState.ASSIGNED and self.source_repository == repository
                and pull_request in self.pull_requests)


def founders_from_env(value: str | None = None) -> frozenset[str]:
    """Parse `ARCHIE_FOUNDER_LOGINS` (comma-separated GitHub logins); invalid entries are dropped."""
    raw = os.getenv("ARCHIE_FOUNDER_LOGINS", "") if value is None else value
    return frozenset(p.strip().lower() for p in raw.split(",") if _LOGIN.match(p.strip()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AssignmentStore:
    def __init__(self, directory: Path, founders: frozenset[str], *,
                 require_authenticated: bool = False, audit: AuditLog | None = None) -> None:
        self._dir = directory
        self._founders = frozenset(f.lower() for f in founders)
        self._require_authenticated = require_authenticated
        self._audit = audit
        self._dir.mkdir(parents=True, exist_ok=True)

    def _require_founder(self, by: str | Actor) -> str:
        if isinstance(by, HumanActor):
            login, proven = by.login, by.role is Role.FOUNDER
        elif isinstance(by, str) and not self._require_authenticated:
            login, proven = by.strip().lower(), True  # legacy CLI form: asserted, not proven
        else:  # an AgentActor, or a bare string where authentication is required
            login, proven = "", False
        if not proven or not self._founders or login not in self._founders:
            self._note(AuditEvent.ASSIGNMENT_REFUSED, login or "unauthenticated", "refused")
            raise NotFounder("only an authenticated, configured founder can assign or cancel curriculum work")
        return login

    def _note(self, event: AuditEvent, actor: str, outcome: str = "ok", assignment_id: str | None = None,
              detail: dict | None = None, key: str | None = None) -> None:
        if self._audit is not None:
            self._audit.append(event, actor, outcome, assignment_id=assignment_id, detail=detail,  # type: ignore[arg-type]
                               key=key)

    def _path(self, assignment_id: str) -> Path:
        if not re.fullmatch(r"AS-[0-9a-f]{12}", assignment_id):
            raise KeyError("unknown assignment")
        return self._dir / f"{assignment_id}.json"

    def _save(self, assignment: Assignment) -> None:
        atomic_write(self._path(assignment.assignment_id),
                     json.dumps(assignment.model_dump(mode="json"), indent=2))

    def get(self, assignment_id: str) -> Assignment | None:
        path = self._path(assignment_id)
        if not path.is_file():
            return None
        return Assignment.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[Assignment]:
        return [Assignment.model_validate_json(p.read_text(encoding="utf-8"))
                for p in sorted(self._dir.glob("AS-*.json"))]

    def assign(self, by: str | Actor, topic: str, source_repository: str,
               pull_requests: tuple[int, ...], *, permitted_evidence: tuple[EvidenceScope, ...] | None = None,
               instructor_paths: tuple[str, ...] = (), idempotency_key: str | None = None) -> Assignment:
        who = self._require_founder(by)
        evidence = tuple(permitted_evidence) if permitted_evidence is not None else DEFAULT_EVIDENCE
        if instructor_paths and EvidenceScope.INSTRUCTOR_GUIDANCE not in evidence:
            raise ValueError("instructor paths need the instructor_guidance evidence scope")
        if EvidenceScope.INSTRUCTOR_GUIDANCE in evidence and not instructor_paths:
            raise ValueError("instructor_guidance evidence needs explicit instructor paths")
        with locked(self._dir / ".lock"):
            if idempotency_key:  # a replayed request returns the original assignment, never a second one
                digest = hashlib.sha256(f"{who}|{idempotency_key}".encode()).hexdigest()[:12]
                existing = self.get(f"AS-{digest}")
                if existing is not None:
                    same = (existing.topic, existing.source_repository, existing.pull_requests) == (
                        topic, source_repository, tuple(pull_requests))
                    if not same:
                        raise ValueError("idempotency key was already used for a different assignment")
                    return existing
            else:
                stamp_digest = hashlib.sha256(
                    f"{who}|{topic}|{source_repository}|{pull_requests}|{_now()}|{os.urandom(8).hex()}".encode())
                digest = stamp_digest.hexdigest()[:12]
            assignment = Assignment(
                assignment_id=f"AS-{digest}", topic=topic, source_repository=source_repository,
                pull_requests=pull_requests, assigned_by=who, assigned_at=_now(),
                permitted_evidence=evidence, instructor_paths=instructor_paths,
                idempotency_key=idempotency_key)
            self._save(assignment)
        self._note(AuditEvent.ASSIGNMENT_CREATED, who, assignment_id=assignment.assignment_id,
                   key=f"assign:{assignment.assignment_id}",
                   detail={"repository": source_repository, "pull_requests": ",".join(map(str, pull_requests)),
                           "evidence": ",".join(e.value for e in evidence)})
        return assignment

    def _close(self, by: str | Actor, assignment_id: str, state: AssignmentState) -> Assignment:
        who = self._require_founder(by)
        with locked(self._dir / ".lock"):
            assignment = self.get(assignment_id)
            if assignment is None:
                raise KeyError("unknown assignment")
            if assignment.state is not AssignmentState.ASSIGNED:
                return assignment  # already closed; closing is idempotent and never reopens
            assignment.state, assignment.closed_by, assignment.closed_at = state, who, _now()
            self._save(assignment)
        event = (AuditEvent.ASSIGNMENT_CANCELLED if state is AssignmentState.CANCELLED
                 else AuditEvent.ASSIGNMENT_COMPLETED)
        self._note(event, who, assignment_id=assignment_id, key=f"close:{assignment_id}")
        return assignment

    def cancel(self, by: str | Actor, assignment_id: str) -> Assignment:
        return self._close(by, assignment_id, AssignmentState.CANCELLED)

    def complete(self, by: str | Actor, assignment_id: str) -> Assignment:
        return self._close(by, assignment_id, AssignmentState.COMPLETED)

    def authorizing(self, repository: str, pull_request: int) -> Assignment | None:
        """The active assignment covering this pull request, or None. Read-only; never creates one."""
        for assignment in self.list():
            if assignment.covers(repository, pull_request):
                return assignment
        return None

    def require_active(self, assignment_id: str) -> Assignment:
        """The named assignment if it is still `assigned`; otherwise NotAssigned."""
        try:
            assignment = self.get(assignment_id)
        except KeyError:
            assignment = None
        if assignment is None or assignment.state is not AssignmentState.ASSIGNED:
            raise NotAssigned("no active founder assignment with that id")
        return assignment

    def require(self, repository: str, pull_request: int) -> Assignment:
        assignment = self.authorizing(repository, pull_request)
        if assignment is None:
            raise NotAssigned("no active founder assignment covers this pull request")
        return assignment
