"""Founder assignments: the only way Archie's curriculum work can start (epic #139, issue #140).

An assignment names a topic and the exact source pull requests Archie may study. Only a login on the
configured founder list can create or cancel one; with no founder list configured, nothing can be
assigned (fail closed). The ingestion pipeline refuses any pull request that no active assignment
covers, so a commit, a merge, a Sage review or a scheduled `reconcile` cannot start a lesson.

Limitation: the founder identity is asserted by whoever runs the local CLI, not cryptographically
proven. The control is that assignments are created only through this module, by an operator with
access to the store directory; no webhook, event handler or agent path imports `assign`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Repo

MAX_PULL_REQUESTS = 20
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class NotFounder(PermissionError):
    """The caller is not on the founder list."""


class NotAssigned(PermissionError):
    """No active founder assignment covers this source revision."""


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

    @field_validator("pull_requests")
    @classmethod
    def _positive_unique(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(n < 1 for n in value) or len(set(value)) != len(value):
            raise ValueError("pull requests must be unique positive numbers")
        return value

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
    def __init__(self, directory: Path, founders: frozenset[str]) -> None:
        self._dir = directory
        self._founders = frozenset(f.lower() for f in founders)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _require_founder(self, login: str) -> str:
        if not self._founders or login.strip().lower() not in self._founders:
            raise NotFounder("only a configured founder can assign or cancel curriculum work")
        return login.strip().lower()

    def _path(self, assignment_id: str) -> Path:
        if not re.fullmatch(r"AS-[0-9a-f]{12}", assignment_id):
            raise KeyError("unknown assignment")
        return self._dir / f"{assignment_id}.json"

    def _save(self, assignment: Assignment) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(assignment.model_dump(mode="json"), handle, indent=2)
        os.replace(tmp, self._path(assignment.assignment_id))

    def get(self, assignment_id: str) -> Assignment | None:
        path = self._path(assignment_id)
        if not path.is_file():
            return None
        return Assignment.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[Assignment]:
        return [Assignment.model_validate_json(p.read_text(encoding="utf-8"))
                for p in sorted(self._dir.glob("AS-*.json"))]

    def assign(self, by: str, topic: str, source_repository: str,
               pull_requests: tuple[int, ...]) -> Assignment:
        who = self._require_founder(by)
        stamp = _now()
        digest = hashlib.sha256(f"{who}|{topic}|{source_repository}|{pull_requests}|{stamp}".encode())
        assignment = Assignment(
            assignment_id=f"AS-{digest.hexdigest()[:12]}", topic=topic,
            source_repository=source_repository, pull_requests=pull_requests,
            assigned_by=who, assigned_at=stamp)
        self._save(assignment)
        return assignment

    def _close(self, by: str, assignment_id: str, state: AssignmentState) -> Assignment:
        who = self._require_founder(by)
        assignment = self.get(assignment_id)
        if assignment is None:
            raise KeyError("unknown assignment")
        if assignment.state is not AssignmentState.ASSIGNED:
            return assignment  # already closed; closing is idempotent and never reopens
        assignment.state, assignment.closed_by, assignment.closed_at = state, who, _now()
        self._save(assignment)
        return assignment

    def cancel(self, by: str, assignment_id: str) -> Assignment:
        return self._close(by, assignment_id, AssignmentState.CANCELLED)

    def complete(self, by: str, assignment_id: str) -> Assignment:
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
