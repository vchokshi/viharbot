"""Lesson lifecycle, review and publication (issues #144, #151).

    draft ──submit──▶ review_pending ──approve──▶ approved ──publish──▶ published ──revoke──▶ revoked
      ▲                    │                         │  ▲
      │                    └─request_changes─▶ changes_requested            approved ──revoke──▶ revoked
      └──────────── revise (draft, changes_requested, approved) ─────────────┘

Who may do what, enforced here in code and never by a prompt or by what a model wrote:

    create / revise / submit   Archie (the agent) or an authenticated human
    request_changes / approve  an authenticated human (founder or reviewer) who is not the submitter;
                               approval needs a recorded attestation that instructional quality and
                               confidentiality were both reviewed
    publish / revoke           an authenticated founder only
    Archie                     can never approve, publish or revoke, and an AgentActor is refused for
                               every one of those whatever it claims

Approval is bound to the exact content: it records the digest of the trainee content and of the
instructor content. Editing an approved draft (`revise`), or finding on disk that the stored files no
longer match the approved digests, clears the approval and returns the lesson to `draft`. Publication
re-reads the stored content, re-verifies the digests, re-runs validation and the confidentiality checks,
and requires the authorising assignment to still be active; any failure refuses and changes nothing
that Ada can see. Revocation first marks the published copy revoked, then removes it, so retrieval
stops at once.

Every transition runs under a per-lesson file lock and re-checks state inside it, so two concurrent
callers cannot both succeed in conflicting ways; repeating a completed transition returns the existing
result without a second audit record.

Limitation: this gates *Archie's* publication path. Publishing into the `viharbot-onboarding`
repository through a GitHub pull request is not wired here; when it is, a human merge must be the
signal and `InstructorProposer`-style classes (which have no merge call) must be used.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ada import PublishedStore, Provenance, build_package, package_texts
from .assignment import AssignmentStore, NotAssigned
from .audit import AuditEvent, AuditLog
from .authn import ARCHIE, Actor, AgentActor, HumanActor, Role, Unauthorized
from .basis import Basis
from .confidentiality import ContentGuard
from .fsutil import atomic_write, locked
from .lesson import InstructorContent, LessonDraft, TraineeContent, digest_of


class LessonState(StrEnum):
    DRAFT = "draft"
    REVIEW_PENDING = "review_pending"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    PUBLISHED = "published"
    REVOKED = "revoked"


class IllegalTransition(ValueError):
    """The lesson is not in a state from which that operation is allowed."""


class ValidationFailed(ValueError):
    """The content failed validation or the confidentiality checks. Carries codes, never text."""

    def __init__(self, codes: list[str]) -> None:
        super().__init__("validation failed: " + ", ".join(codes))
        self.codes = codes


class IntegrityError(ValueError):
    """Stored content no longer matches the digests that were recorded."""


LEGAL: dict[str, tuple[frozenset[LessonState], LessonState | None]] = {
    "submit": (frozenset({LessonState.DRAFT}), LessonState.REVIEW_PENDING),
    "request_changes": (frozenset({LessonState.REVIEW_PENDING}), LessonState.CHANGES_REQUESTED),
    "approve": (frozenset({LessonState.REVIEW_PENDING}), LessonState.APPROVED),
    "revise": (frozenset({LessonState.DRAFT, LessonState.CHANGES_REQUESTED, LessonState.APPROVED}),
               LessonState.DRAFT),
    "publish": (frozenset({LessonState.APPROVED}), LessonState.PUBLISHED),
    "revoke": (frozenset({LessonState.APPROVED, LessonState.PUBLISHED}), LessonState.REVOKED),
}


class _F(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Attestation(_F):
    instructional_quality_reviewed: bool
    confidentiality_reviewed: bool


class Approval(_F):
    approver: str
    role: str
    approved_at: str
    revision: int
    trainee_digest: str
    instructor_digest: str
    attestation: Attestation


class Step(_F):
    at: str
    op: str
    actor: str
    from_state: str
    to_state: str


class LessonRecord(_F):
    lesson_id: str
    version: int
    state: LessonState = LessonState.DRAFT
    revision: int = 1
    assignment_id: str
    workflow_id: str
    source_sha: str
    trainee_digest: str
    instructor_digest: str
    submitted_by: str | None = None
    approval: Approval | None = None
    published_by: str | None = None
    published_at: str | None = None
    history: list[Step] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DraftStore:
    """Archie's side: `<root>/<lesson_id>/v<N>/{trainee,instructor,record}.json`. Directory mode 0700.

    Trainee and instructor content are separate files, so a reader that only needs one cannot load
    the other. Nothing here is ever served to Ada."""

    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)

    def dir(self, lesson_id: str, version: int) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", lesson_id) or version < 1:
            raise KeyError("unknown lesson")
        return self._root / lesson_id / f"v{version}"

    def lock(self, lesson_id: str, version: int) -> Path:
        return self.dir(lesson_id, version).parent / f".v{version}.lock"

    def record(self, lesson_id: str, version: int) -> LessonRecord | None:
        path = self.dir(lesson_id, version) / "record.json"
        return LessonRecord.model_validate_json(path.read_text("utf-8")) if path.is_file() else None

    def save_record(self, record: LessonRecord) -> None:
        atomic_write(self.dir(record.lesson_id, record.version) / "record.json", record.model_dump_json(indent=2))

    def save_content(self, draft: LessonDraft) -> None:
        d = self.dir(draft.lesson_id, draft.version)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write(d / "trainee.json", draft.trainee.model_dump_json(indent=2))
        atomic_write(d / "instructor.json", draft.instructor.model_dump_json(indent=2))
        atomic_write(d / "draft_meta.json", draft.model_dump_json(exclude={"trainee", "instructor"}))

    def load(self, lesson_id: str, version: int) -> LessonDraft:
        d = self.dir(lesson_id, version)
        meta = json.loads((d / "draft_meta.json").read_text("utf-8"))
        meta["trainee"] = json.loads((d / "trainee.json").read_text("utf-8"))
        meta["instructor"] = json.loads((d / "instructor.json").read_text("utf-8"))
        return LessonDraft.model_validate(meta)

    def versions(self, lesson_id: str) -> list[int]:
        base = self.dir(lesson_id, 1).parent
        return sorted(int(p.name[1:]) for p in base.glob("v*") if p.is_dir() and p.name[1:].isdigit()) \
            if base.is_dir() else []

    def remove_version(self, lesson_id: str, version: int) -> None:
        shutil.rmtree(self.dir(lesson_id, version), ignore_errors=True)


def validation_codes(draft: LessonDraft, guard: ContentGuard | None = None) -> list[str]:
    """Reasons a draft may not enter review, approval or publication. Empty means it may.

    Codes name the check, never the text that tripped it."""
    codes: list[str] = []
    try:
        LessonDraft.model_validate(draft.model_dump())
    except ValidationError:
        return ["schema"]
    t = draft.trainee
    if any(s.basis is Basis.UNSUPPORTED for s in t.statements()):
        codes.append("unsupported_statement")  # an unsupported conclusion belongs in open_questions
    texts = t.texts()
    scan = guard or ContentGuard()
    kinds = {f.kind for text in texts for f in scan.check(text)}
    codes += [f"confidentiality:{k}" for k in sorted(kinds)]
    # Structured check: nothing the instructor side holds may reappear in trainee-facing text.
    mine = ContentGuard()
    for text in (*draft.instructor.evaluation_guidance, *(a.answer for a in draft.instructor.expected_answers)):
        mine.add(text)
    # Copying only (exact line or shingle): generic guidance legitimately resembles the lesson it grades.
    if any(f.kind in ("exact_line", "shingle") for text in texts for f in mine.check(text)):
        codes.append("instructor_content_in_trainee")
    return codes


class PublicationService:
    def __init__(self, drafts: DraftStore, published: PublishedStore, assignments: AssignmentStore,
                 audit: AuditLog, guard: ContentGuard | None = None) -> None:
        self._drafts, self._published, self._assignments = drafts, published, assignments
        self._audit, self._guard = audit, guard

    # -- helpers -----------------------------------------------------------------------------
    def _refuse(self, op: str, actor: Actor, record: LessonRecord | None, reason: str,
                exc: Exception) -> Exception:
        self._audit.append(AuditEvent.REJECTED, actor.login, "refused",
                           assignment_id=record.assignment_id if record else None,
                           workflow_id=record.workflow_id if record else None,
                           artifact_id=record.lesson_id if record else None,
                           source_revision=record.source_sha if record else None,
                           detail={"op": op, "reason": reason})
        return exc

    def _need_drafter(self, op: str, actor: Actor, record: LessonRecord | None = None) -> None:
        ok = (isinstance(actor, HumanActor) or (isinstance(actor, AgentActor) and actor == ARCHIE))
        if not ok:
            raise self._refuse(op, actor, record, "actor_not_permitted", Unauthorized("not permitted"))

    def _need_human(self, op: str, actor: Actor, roles: set[Role], record: LessonRecord) -> HumanActor:
        if not isinstance(actor, HumanActor) or actor.role not in roles:
            raise self._refuse(op, actor, record, "human_role_required", Unauthorized(
                f"{op} requires an authenticated {'/'.join(sorted(r.value for r in roles))}"))
        return actor

    def _require_assignment(self, record: LessonRecord, op: str, actor: Actor) -> None:
        try:
            self._assignments.require_active(record.assignment_id)
        except NotAssigned as exc:
            raise self._refuse(op, actor, record, "assignment_inactive", exc) from None

    def _get(self, lesson_id: str, version: int) -> LessonRecord:
        record = self._drafts.record(lesson_id, version)
        if record is None:
            raise KeyError("unknown lesson")
        return record

    def _legal(self, op: str, record: LessonRecord, actor: Actor) -> LessonState:
        allowed, target = LEGAL[op]
        if record.state not in allowed:
            raise self._refuse(op, actor, record, f"illegal_from_{record.state.value}", IllegalTransition(
                f"cannot {op} a lesson that is {record.state.value}"))
        assert target is not None
        return target

    def _move(self, record: LessonRecord, op: str, actor: Actor, to: LessonState) -> None:
        record.history.append(Step(at=_now(), op=op, actor=actor.login, from_state=record.state.value,
                                   to_state=to.value))
        record.state = to
        self._drafts.save_record(record)

    def _verify_stored(self, record: LessonRecord, actor: Actor, op: str) -> LessonDraft:
        """Reload the stored content and require it to match the recorded digests; else invalidate."""
        draft = self._drafts.load(record.lesson_id, record.version)
        if (digest_of(draft.trainee), digest_of(draft.instructor)) != (record.trainee_digest, record.instructor_digest):
            if record.state in (LessonState.DRAFT, LessonState.CHANGES_REQUESTED):
                # Never reviewed in this form (for example an interrupted revise): adopt what is on disk.
                record.trainee_digest, record.instructor_digest = digest_of(draft.trainee), digest_of(draft.instructor)
                record.revision += 1
                self._drafts.save_record(record)
                return draft
            if record.approval is not None or record.state in (LessonState.APPROVED, LessonState.REVIEW_PENDING):
                record.approval, record.revision = None, record.revision + 1
                record.trainee_digest, record.instructor_digest = digest_of(draft.trainee), digest_of(draft.instructor)
                self._move(record, "invalidate", actor, LessonState.DRAFT)
                self._audit.append(AuditEvent.APPROVAL_INVALIDATED, actor.login, "refused",
                                   assignment_id=record.assignment_id, workflow_id=record.workflow_id,
                                   artifact_id=record.lesson_id, source_revision=record.source_sha,
                                   detail={"op": op, "reason": "content_modified", "revision": record.revision})
            raise IntegrityError("stored content no longer matches its recorded digests")
        return draft

    # -- drafting ----------------------------------------------------------------------------
    def create_draft(self, draft: LessonDraft, actor: Actor) -> LessonRecord:
        """Store a new draft version. Repeating it with identical content returns the same record."""
        self._need_drafter("create_draft", actor)
        a = self._assignments.require_active(draft.assignment_id)
        if not a.covers(draft.source_repository, draft.pull_request):
            raise NotAssigned("assignment does not cover this pull request")
        with locked(self._drafts.lock(draft.lesson_id, draft.version)):
            existing = self._drafts.record(draft.lesson_id, draft.version)
            digests = (digest_of(draft.trainee), digest_of(draft.instructor))
            if existing is not None:
                if digests == (existing.trainee_digest, existing.instructor_digest):
                    return existing
                raise IllegalTransition("this version already exists with different content; use revise")
            record = LessonRecord(lesson_id=draft.lesson_id, version=draft.version,
                                  assignment_id=draft.assignment_id, workflow_id=draft.workflow_id,
                                  source_sha=draft.source_sha, trainee_digest=digests[0],
                                  instructor_digest=digests[1])
            self._drafts.save_content(draft)
            self._drafts.save_record(record)
        self._audit.append(AuditEvent.DRAFT_CREATED, actor.login, assignment_id=draft.assignment_id,
                           workflow_id=draft.workflow_id, artifact_id=draft.lesson_id,
                           source_revision=draft.source_sha, key=f"draft:{draft.lesson_id}:v{draft.version}:{digests[0][:12]}",
                           detail={"version": draft.version, "origin": draft.origin,
                                   "digest": digests[0][:16]})
        return record

    def revise(self, lesson_id: str, version: int, draft: LessonDraft, actor: Actor) -> LessonRecord:
        self._need_drafter("revise", actor)
        if (draft.lesson_id, draft.version) != (lesson_id, version):
            raise ValueError("revision must keep the lesson id and version")
        with locked(self._drafts.lock(lesson_id, version)):
            record = self._get(lesson_id, version)
            self._legal("revise", record, actor)
            self._require_assignment(record, "revise", actor)
            if (draft.assignment_id, draft.source_sha) != (record.assignment_id, record.source_sha):
                raise ValueError("revision must keep the assignment and source revision")
            had_approval = record.approval is not None
            record.approval, record.revision = None, record.revision + 1
            record.trainee_digest, record.instructor_digest = digest_of(draft.trainee), digest_of(draft.instructor)
            record.submitted_by = None
            self._drafts.save_content(draft)
            self._move(record, "revise", actor, LessonState.DRAFT)
        if had_approval:
            self._audit.append(AuditEvent.APPROVAL_INVALIDATED, actor.login, assignment_id=record.assignment_id,
                               workflow_id=record.workflow_id, artifact_id=lesson_id,
                               source_revision=record.source_sha, key=f"invalidate:{lesson_id}:v{version}:r{record.revision}",
                               detail={"reason": "revised_after_approval", "revision": record.revision})
        self._audit.append(AuditEvent.DRAFT_REVISED, actor.login, assignment_id=record.assignment_id,
                           workflow_id=record.workflow_id, artifact_id=lesson_id, source_revision=record.source_sha,
                           key=f"revise:{lesson_id}:v{version}:r{record.revision}",
                           detail={"revision": record.revision, "digest": record.trainee_digest[:16]})
        return record

    # -- review ------------------------------------------------------------------------------
    def submit(self, lesson_id: str, version: int, actor: Actor) -> LessonRecord:
        self._need_drafter("submit", actor)
        with locked(self._drafts.lock(lesson_id, version)):
            record = self._get(lesson_id, version)
            if record.state is LessonState.REVIEW_PENDING and record.submitted_by == actor.login:
                return record  # idempotent retry
            to = self._legal("submit", record, actor)
            self._require_assignment(record, "submit", actor)
            draft = self._verify_stored(record, actor, "submit")
            codes = validation_codes(draft, self._guard)
            if codes:
                raise self._refuse("submit", actor, record, "validation", ValidationFailed(codes))
            record.submitted_by = actor.login
            self._move(record, "submit", actor, to)
        self._audit.append(AuditEvent.REVIEW_SUBMITTED, actor.login, assignment_id=record.assignment_id,
                           workflow_id=record.workflow_id, artifact_id=lesson_id, source_revision=record.source_sha,
                           key=f"submit:{lesson_id}:v{version}:r{record.revision}",
                           detail={"revision": record.revision})
        return record

    def request_changes(self, lesson_id: str, version: int, actor: Actor, reason_code: str = "unspecified") -> LessonRecord:
        with locked(self._drafts.lock(lesson_id, version)):
            record = self._get(lesson_id, version)
            self._need_human("request_changes", actor, {Role.FOUNDER, Role.REVIEWER}, record)
            if record.state is LessonState.CHANGES_REQUESTED:
                return record
            to = self._legal("request_changes", record, actor)
            self._move(record, "request_changes", actor, to)
        self._audit.append(AuditEvent.REVIEW_CHANGES_REQUESTED, actor.login, assignment_id=record.assignment_id,
                           workflow_id=record.workflow_id, artifact_id=lesson_id, source_revision=record.source_sha,
                           key=f"changes:{lesson_id}:v{version}:r{record.revision}",
                           detail={"reason_code": re.sub(r"[^a-z_]", "", reason_code.lower())[:40] or "unspecified"})
        return record

    def approve(self, lesson_id: str, version: int, actor: Actor, attestation: Attestation) -> LessonRecord:
        with locked(self._drafts.lock(lesson_id, version)):
            record = self._get(lesson_id, version)
            human = self._need_human("approve", actor, {Role.FOUNDER, Role.REVIEWER}, record)
            if record.state is LessonState.APPROVED and record.approval and record.approval.approver == human.login:
                return record
            to = self._legal("approve", record, actor)
            if human.login == record.submitted_by:
                raise self._refuse("approve", actor, record, "self_approval", Unauthorized(
                    "the submitter cannot approve their own submission"))
            if not (attestation.instructional_quality_reviewed and attestation.confidentiality_reviewed):
                raise self._refuse("approve", actor, record, "attestation_incomplete", ValidationFailed(
                    ["attestation_incomplete"]))
            draft = self._verify_stored(record, actor, "approve")
            codes = validation_codes(draft, self._guard)
            if codes:
                raise self._refuse("approve", actor, record, "validation", ValidationFailed(codes))
            record.approval = Approval(approver=human.login, role=human.role.value, approved_at=_now(),
                                       revision=record.revision, trainee_digest=record.trainee_digest,
                                       instructor_digest=record.instructor_digest, attestation=attestation)
            self._move(record, "approve", actor, to)
        self._audit.append(AuditEvent.APPROVED, actor.login, assignment_id=record.assignment_id,
                           workflow_id=record.workflow_id, artifact_id=lesson_id, source_revision=record.source_sha,
                           key=f"approve:{lesson_id}:v{version}:r{record.revision}",
                           detail={"revision": record.revision, "digest": record.trainee_digest[:16],
                                   "role": human.role.value})
        return record

    # -- publication -------------------------------------------------------------------------
    def publish(self, lesson_id: str, version: int, actor: Actor) -> LessonRecord:
        with locked(self._drafts.lock(lesson_id, version)):
            record = self._get(lesson_id, version)
            human = self._need_human("publish", actor, {Role.FOUNDER}, record)
            if record.state is LessonState.PUBLISHED and self._published.state(lesson_id, version) == "published":
                return record  # idempotent: already published, nothing repeated
            self._legal("publish", record, actor)
            self._require_assignment(record, "publish", actor)
            approval = record.approval
            if approval is None or (approval.trainee_digest, approval.instructor_digest, approval.revision) != (
                    record.trainee_digest, record.instructor_digest, record.revision):
                raise self._refuse("publish", actor, record, "approval_stale", Unauthorized("approval is not current"))
            draft = self._verify_stored(record, actor, "publish")
            codes = validation_codes(draft, self._guard)
            try:
                package = build_package(lesson_id, version, draft.trainee, Provenance(
                    source_repository=draft.source_repository, pull_request=draft.pull_request,
                    source_sha=draft.source_sha, artifact_version=draft.artifact_version,
                    review_refs=draft.review_refs, evidence_confidence=draft.trainee.evidence_confidence,
                    approved_by=approval.approver, approved_at=approval.approved_at,
                    approval_revision=approval.revision, published_by=human.login, published_at=_now()))
            except ValidationError:
                raise self._refuse("publish", actor, record, "package_schema", ValidationFailed(["package_schema"])) from None
            scan = self._guard or ContentGuard()
            if any(scan.check(t) for t in package_texts(package)):
                codes.append("confidentiality:package")
            if codes:
                raise self._refuse("publish", actor, record, "validation", ValidationFailed(codes))
            self._published.publish(package)  # Ada can read it from here
            record.published_by, record.published_at = human.login, _now()
            self._move(record, "publish", actor, LessonState.PUBLISHED)
        self._audit.append(AuditEvent.PUBLISHED, actor.login, assignment_id=record.assignment_id,
                           workflow_id=record.workflow_id, artifact_id=lesson_id, source_revision=record.source_sha,
                           key=f"publish:{lesson_id}:v{version}:r{record.revision}",
                           detail={"version": version, "content_digest": package.integrity.content_digest[:16]})
        return record

    def revoke(self, lesson_id: str, version: int, actor: Actor) -> LessonRecord:
        with locked(self._drafts.lock(lesson_id, version)):
            record = self._get(lesson_id, version)
            self._need_human("revoke", actor, {Role.FOUNDER}, record)
            if record.state is LessonState.REVOKED:
                self._published.revoke(lesson_id, version)  # make sure the copy is gone after an interrupted run
                return record
            to = self._legal("revoke", record, actor)
            self._published.revoke(lesson_id, version)  # first: Ada can no longer retrieve it
            self._move(record, "revoke", actor, to)
        self._audit.append(AuditEvent.REVOKED, actor.login, assignment_id=record.assignment_id,
                           workflow_id=record.workflow_id, artifact_id=lesson_id, source_revision=record.source_sha,
                           key=f"revoke:{lesson_id}:v{version}", detail={"version": version})
        return record

    def status(self, lesson_id: str, version: int) -> dict[str, str | int | bool | None]:
        """Identifiers, state and digests only: no lesson or instructor text."""
        r = self._get(lesson_id, version)
        return {"lesson_id": r.lesson_id, "version": r.version, "state": r.state.value, "revision": r.revision,
                "assignment_id": r.assignment_id, "workflow_id": r.workflow_id, "source_sha": r.source_sha,
                "trainee_digest": r.trainee_digest[:16], "approved_by": r.approval.approver if r.approval else None,
                "published": self._published.state(lesson_id, version) == "published"}
