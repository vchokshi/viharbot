"""Archie's curriculum workflow, end to end (epic #139).

    founder assignment → evidence → draft → review → approval → publication → Ada retrieval

This module sequences the steps; every authorization decision stays in the modules it calls
(`AssignmentStore`, `PublicationService`), so nothing here can be talked into skipping one. It adds:

- a persisted `WorkflowRecord` per assignment, pull request and source commit, so an interrupted run
  resumes from the last completed step and a repeated call changes nothing;
- failure recording (an exception class name only) without losing the record, and a refusal for
  anything the assignment or the lifecycle does not allow (those errors propagate to the caller);
- `status()`, which shows state, identifiers and digests and never lesson or instructor text.

Nothing starts by itself: the only entry point is `start`/`run_to_draft` with an active founder
assignment, and nothing in the web app, an event handler or an agent imports this module.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .assignment import AssignmentStore
from .audit import AuditEvent, AuditLog
from .authn import Actor
from .confidentiality import ContentGuard
from .evidence import EvidenceBundle, KeplerHandoffProvider, collect_evidence
from .fsutil import atomic_write, locked
from .github import SourceReader
from .instructor_access import InstructorReader
from .lesson import digest_of
from .publication import Attestation, LessonState, PublicationService
from .synthesis import ModelClient, build_baseline, synthesize

Step = Literal["collected", "drafted", "submitted", "approved", "published", "revoked"]


class WorkflowRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str
    assignment_id: str
    repository: str
    pull_request: int
    source_sha: str
    step: Step
    evidence_digest: str
    lesson_id: str | None = None
    version: int | None = None
    last_error: str | None = None  # an exception class name only
    attempts: int = 0
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CurriculumWorkflow:
    def __init__(self, directory: Path, *, assignments: AssignmentStore, service: PublicationService,
                 audit: AuditLog, reader: SourceReader, kepler: KeplerHandoffProvider | None = None,
                 instructor: InstructorReader | None = None, model: ModelClient | None = None,
                 guard: ContentGuard | None = None) -> None:
        self._dir = directory
        directory.mkdir(parents=True, exist_ok=True)
        self._assignments, self._service, self._audit, self._reader = assignments, service, audit, reader
        self._kepler, self._instructor, self._model, self._guard = kepler, instructor, model, guard

    # -- persistence -------------------------------------------------------------------------
    def _path(self, workflow_id: str, suffix: str = "json") -> Path:
        if not re.fullmatch(r"WF-[0-9a-f]{12}", workflow_id):
            raise KeyError("unknown workflow")
        return self._dir / f"{workflow_id}.{suffix}"

    def get(self, workflow_id: str) -> WorkflowRecord | None:
        path = self._path(workflow_id)
        return WorkflowRecord.model_validate_json(path.read_text("utf-8")) if path.is_file() else None

    def _save(self, record: WorkflowRecord) -> None:
        record.updated_at = _now()
        atomic_write(self._path(record.workflow_id), record.model_dump_json(indent=2))

    def _bundle(self, workflow_id: str) -> EvidenceBundle:
        # Instructor text is excluded from serialisation, so this file cannot hold it.
        return EvidenceBundle.model_validate_json(self._path(workflow_id, "evidence.json").read_text("utf-8"))

    # -- steps -------------------------------------------------------------------------------
    def start(self, assignment_id: str, pull_request: int, actor: Actor) -> WorkflowRecord:
        """Collect evidence under an active assignment. Safe to repeat; refreshes until drafted."""
        self._service._need_drafter("start", actor)
        bundle = collect_evidence(assignment_id, pull_request, reader=self._reader, assignments=self._assignments,
                                  audit=self._audit, kepler=self._kepler, instructor=self._instructor,
                                  actor=actor.login)
        wf = bundle.workflow_id
        with locked(self._dir / f".{wf}.lock"):
            existing = self.get(wf)
            if existing is not None and existing.step not in ("collected", "drafted"):
                return existing  # work has moved past evidence; do not rewrite what was reviewed
            atomic_write(self._path(wf, "evidence.json"), bundle.model_dump_json(indent=2))
            record = existing or WorkflowRecord(
                workflow_id=wf, assignment_id=assignment_id, repository=bundle.repository,
                pull_request=pull_request, source_sha=bundle.source_sha, step="collected",
                evidence_digest=bundle.digest(), updated_at=_now())
            record.evidence_digest, record.last_error = bundle.digest(), None
            self._save(record)
        return record

    def draft(self, workflow_id: str, actor: Actor) -> WorkflowRecord:
        with locked(self._dir / f".{workflow_id}.lock"):
            record = self.get(workflow_id)
            if record is None:
                raise KeyError("unknown workflow")
            if record.step not in ("collected", "drafted"):
                return record
            self._assignments.require_active(record.assignment_id)  # cancelled mid-flight: stop here
            bundle = self._bundle(workflow_id)
            drafts = self._service._drafts
            lesson_id = build_baseline(bundle).lesson_id
            versions = drafts.versions(lesson_id)
            latest = drafts.record(lesson_id, versions[-1]) if versions else None
            version = 1 if latest is None else latest.version
            if latest is not None and latest.state in (LessonState.PUBLISHED, LessonState.REVOKED):
                version = latest.version + 1
            candidate = synthesize(bundle, build_baseline(bundle, version), self._model, self._guard,
                                   self._audit, actor.login)
            if latest is None or version != latest.version:
                self._service.create_draft(candidate, actor)
            elif (digest_of(candidate.trainee), digest_of(candidate.instructor)) != (
                    latest.trainee_digest, latest.instructor_digest):
                if latest.state is LessonState.REVIEW_PENDING:
                    return record  # a reviewer is looking at it; do not move it underneath them
                self._service.revise(lesson_id, version, candidate, actor)
            record.lesson_id, record.version, record.step, record.last_error = lesson_id, version, "drafted", None
            self._save(record)
        return record

    def run_to_draft(self, assignment_id: str, pull_request: int, actor: Actor) -> WorkflowRecord:
        """`start` then `draft`. A non-authorization failure after evidence was collected is recorded on the
        workflow and returned, so a retry resumes; before any record exists it is audited and re-raised."""
        try:
            record = self.start(assignment_id, pull_request, actor)
            return self.draft(record.workflow_id, actor)
        except PermissionError:
            raise  # not assigned, not authorised: the caller must see it
        except Exception as exc:
            # Look the record up now: `start` may have created it before the failure.
            return self._fail(self._find(assignment_id, pull_request), assignment_id, pull_request, exc, actor)

    def _find(self, assignment_id: str, pull_request: int) -> str | None:
        """The workflow id already recorded for this assignment and pull request, if any."""
        for path in sorted(self._dir.glob("WF-*.json")):
            if path.name.count(".") == 1:
                rec = WorkflowRecord.model_validate_json(path.read_text("utf-8"))
                if (rec.assignment_id, rec.pull_request) == (assignment_id, pull_request):
                    return rec.workflow_id
        return None

    def _fail(self, wf: str | None, assignment_id: str, pull_request: int, exc: Exception, actor: Actor) -> WorkflowRecord:
        code = type(exc).__name__
        self._audit.append(AuditEvent.WORKFLOW_FAILED, actor.login, "error", assignment_id=assignment_id,
                           workflow_id=wf, detail={"error": code, "pull_request": pull_request})
        record = self.get(wf) if wf else None
        if record is None:
            raise exc
        record.last_error, record.attempts = code, record.attempts + 1
        self._save(record)
        return record

    def _lesson(self, workflow_id: str) -> tuple[WorkflowRecord, str, int]:
        record = self.get(workflow_id)
        if record is None or record.lesson_id is None or record.version is None:
            raise KeyError("no draft for this workflow")
        return record, record.lesson_id, record.version

    def submit(self, workflow_id: str, actor: Actor) -> WorkflowRecord:
        record, lid, ver = self._lesson(workflow_id)
        self._service.submit(lid, ver, actor)
        return self._advance(record, "submitted")

    def approve(self, workflow_id: str, actor: Actor, attestation: Attestation) -> WorkflowRecord:
        record, lid, ver = self._lesson(workflow_id)
        self._service.approve(lid, ver, actor, attestation)
        return self._advance(record, "approved")

    def request_changes(self, workflow_id: str, actor: Actor, reason_code: str = "unspecified") -> WorkflowRecord:
        record, lid, ver = self._lesson(workflow_id)
        self._service.request_changes(lid, ver, actor, reason_code)
        return self._advance(record, "drafted")

    def publish(self, workflow_id: str, actor: Actor) -> WorkflowRecord:
        record, lid, ver = self._lesson(workflow_id)
        self._service.publish(lid, ver, actor)
        return self._advance(record, "published")

    def revoke(self, workflow_id: str, actor: Actor) -> WorkflowRecord:
        record, lid, ver = self._lesson(workflow_id)
        self._service.revoke(lid, ver, actor)
        return self._advance(record, "revoked")

    def _advance(self, record: WorkflowRecord, step: Step) -> WorkflowRecord:
        with locked(self._dir / f".{record.workflow_id}.lock"):
            fresh = self.get(record.workflow_id) or record
            fresh.step, fresh.last_error = step, None
            self._save(fresh)
        return fresh

    def status(self, workflow_id: str) -> dict[str, object]:
        """Where the workflow stands. Identifiers, states and digests only; no lesson text."""
        record = self.get(workflow_id)
        if record is None:
            raise KeyError("unknown workflow")
        assignment = self._assignments.get(record.assignment_id)
        out: dict[str, object] = {
            "workflow_id": record.workflow_id, "step": record.step, "assignment_id": record.assignment_id,
            "assignment_state": assignment.state.value if assignment else "unknown",
            "repository": record.repository, "pull_request": record.pull_request,
            "source_sha": record.source_sha, "evidence_digest": record.evidence_digest[:16],
            "attempts": record.attempts, "last_error": record.last_error,
            "audit_records": len(self._audit.records(workflow_id=workflow_id))}
        if record.lesson_id and record.version:
            out["lesson"] = self._service.status(record.lesson_id, record.version)
        return out

