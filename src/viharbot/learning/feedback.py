"""Student pull-request feedback after Sage's review (issue #154; delivery contract for Ada, #155).

This is a separate workflow from curriculum authoring and shares none of its code path: it does not
import the lesson, evidence or publication modules, never creates an assignment, a lesson or a
package for the curriculum, and cannot grant access, approve or merge anything.

What Archie prepares: student-safe learning feedback tied to one exact commit. It is built only from
the pull request's own text and a *trusted, complete, current* Sage review of that same commit:

    refused unless   the author is on the founder-maintained student roster
                     a trusted Sage review exists, is `complete`, and reviewed the PR's final head
    explains         what the student attempted (their own words, labelled as such), what Sage observed
                     (Sage's labels, advisory), why the concept matters, suggested activities, and what
                     to consider revising
    never claims     that a finding was resolved: it says resolution is not determined and asks for a
                     new commit and a Sage re-review. Sage stays the code reviewer.

Lifecycle (code-enforced): draft → review_pending → approved → released; `stale` when the PR head moves
past the reviewed commit (from any live state); `revoked` by the founder. Archie can prepare and submit.
A human who is not the submitter approves, with a quality-and-confidentiality attestation; only a
founder releases, and release re-checks the live head commit. Retrieval (`FeedbackGateway`, the only
thing Ada holds) returns a released, current package and only to the student it was written for.
No instructor material is an input, so none can appear; the confidentiality checks run at release.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .audit import AuditEvent, AuditLog
from .authn import ARCHIE, Actor, AgentActor, HumanActor, Role, Unauthorized
from .confidentiality import ContentGuard
from .fsutil import atomic_write, locked
from .models import SageEvidence, Sha, StudentPRSnapshot

Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
NOTICES = (
    "Sage reviews the code. The observations below are Sage's advisory opinion, not verified fact.",
    "Whether any observation is now handled is not determined here. Push a new commit and ask for a "
    "Sage re-review of it.",
)
CONCEPTS: tuple[tuple[str, str, str, str], ...] = (
    (r"secur|auth|secret|inject|privacy", "Trust boundaries and untrusted input",
     "Code that handles credentials or outside input is where a small mistake becomes a real exposure.",
     "Write down who or what supplies each input this change touches and what it is allowed to do."),
    (r"test|coverage", "Testing behaviour, not just code paths",
     "Tests are how a reviewer knows a change keeps working after the next change.",
     "Add one test that fails without your change and passes with it."),
    (r"correct|bug|logic|edge", "Correctness and edge cases",
     "A change can look right on the usual input and still fail on the unusual one.",
     "List three unusual inputs and trace what your code does with each."),
    (r"doc|readme|comment", "Documentation as part of the change",
     "A change nobody can understand is hard to review and harder to maintain.",
     "Explain the change to a teammate in three sentences, then put those sentences in the description."),
    (r"maintain|style|complex|design|architect", "Design and maintainability",
     "Simple, consistent code is cheaper to review, test and change later.",
     "Describe one simpler way to structure this change and what you would lose by using it."),
)
DEFAULT_CONCEPT = ("Responding to review",
                   "Review is how a team catches what the author cannot see.",
                   "For each observation, write what you think it means and what evidence would show it is handled.")
_RESOLVED = re.compile(r"\b(?:has|have|had|was|were|is|are|now)\s+(?:been\s+)?(?:resolved|fixed|addressed)\b", re.I)


class NotEligible(PermissionError):
    """The pull request or its Sage review does not qualify for feedback."""


class FeedbackState(StrEnum):
    DRAFT = "draft"
    REVIEW_PENDING = "review_pending"
    APPROVED = "approved"
    RELEASED = "released"
    STALE = "stale"
    REVOKED = "revoked"


class _F(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Observation(_F):
    severity: Short
    category: Short
    location: Short | None = None
    sage_description: Text  # Sage's own words, quoted as advisory
    basis: Literal["sage_advisory"] = "sage_advisory"
    concept: Short
    why_it_matters: Text


class FeedbackContent(_F):
    student_login: Short
    repository: Short
    pull_request: int
    reviewed_sha: Sha
    sage_review_ref: Short
    sage_review_digest: str
    attempted: Text  # the student's own statement, labelled as such
    attempted_basis: Literal["author_statement"] = "author_statement"
    observations: tuple[Observation, ...]
    findings_omitted: int = 0
    learning_activities: tuple[Short, ...]
    consider_revising: tuple[Short, ...]
    notices: tuple[Short, ...] = NOTICES


class Segment(_F):
    order: int = Field(ge=1)
    title: Short
    narration: Text


class FeedbackPackage(_F):
    """The contract Ada's feedback delivery reads (issue #155). Scripted; Ada adds nothing."""

    contract_version: Literal["1.0"] = "1.0"
    feedback_id: str
    student_login: str
    repository: str
    pull_request: int
    reviewed_sha: Sha
    review_version: str  # digest of the Sage review it rests on
    segments: tuple[Segment, ...]
    approved_by: str
    released_by: str
    released_at: str
    content_digest: str
    delivery_mode: Literal["scripted"] = "scripted"

    def verify(self) -> bool:
        return self.content_digest == _package_digest(self)


class Step(_F):
    at: str
    op: str
    actor: str
    to_state: str


class FeedbackRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feedback_id: str
    repository: str
    pull_request: int
    student_login: str
    reviewed_sha: str
    sage_review_digest: str
    state: FeedbackState = FeedbackState.DRAFT
    content_digest: str
    submitted_by: str | None = None
    approved_by: str | None = None
    released_by: str | None = None
    stale_reason: str | None = None
    history: list[Step] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def feedback_id_for(repository: str, pull_request: int, reviewed_sha: str) -> str:
    return "FB-" + _sha(repository, str(pull_request), reviewed_sha)[:12]


def _package_digest(p: FeedbackPackage) -> str:
    return _sha(p.feedback_id, p.student_login, p.reviewed_sha, p.review_version,
                *(f"{s.order}:{s.title}:{s.narration}" for s in p.segments))


def roster_from_env(value: str | None = None) -> frozenset[str]:
    import os
    raw = os.getenv("ARCHIE_STUDENT_LOGINS", "") if value is None else value
    return frozenset(p.strip().lower() for p in raw.split(",")
                     if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", p.strip()))


def sage_digest(review: SageEvidence) -> str:
    return _sha(review.reviewed_sha, review.completeness, review.recommendation,
                *(f"{f.severity}:{f.category}:{f.location}:{f.description}" for f in review.findings))


def build_feedback(snapshot: StudentPRSnapshot, roster: frozenset[str]) -> FeedbackContent:
    """Student-safe feedback for this exact commit, or `NotEligible`. Pure; no I/O."""
    if snapshot.author_login.lower() not in roster:
        raise NotEligible("the pull request author is not on the student roster")
    review = snapshot.sage_review
    if review is None:
        raise NotEligible("no trusted Sage review of this pull request")
    if review.completeness != "complete":
        raise NotEligible("Sage's review is not complete")
    if review.reviewed_head_current is not True or review.reviewed_sha != snapshot.head_sha:
        raise NotEligible("Sage's review is not of the pull request's current head commit")
    observations = []
    for f in review.findings:
        concept, why, _ = next(((c, w, a) for rx, c, w, a in CONCEPTS if re.search(rx, f.category, re.I)),
                               DEFAULT_CONCEPT)
        observations.append(Observation(severity=f.severity, category=f.category[:300], location=f.location,
                                        sage_description=f.description or "(no description)", concept=concept,
                                        why_it_matters=why))
    activities = []
    for f in review.findings:
        act = next((a for rx, _, _, a in CONCEPTS if re.search(rx, f.category, re.I)), DEFAULT_CONCEPT[2])
        if act not in activities:
            activities.append(act)
    activities = activities or ["Re-read your description and check that it says why you made the change."]
    revising = [f"Consider the {o.severity} observation on {o.category}"
                + (f" at {o.location}" if o.location else "") + "; decide what it means for your change."
                for o in observations] or ["Sage recorded no findings; check your own description and tests once more."]
    attempted = (snapshot.title.strip() or "(untitled)") + (
        f" — {snapshot.body.strip()[:1200]}" if snapshot.body.strip() else "")
    return FeedbackContent(
        student_login=snapshot.author_login.lower(), repository=snapshot.repository, pull_request=snapshot.number,
        reviewed_sha=snapshot.head_sha, sage_review_ref=(review.comment_url or "comment URL not retrieved")[:300],
        sage_review_digest=sage_digest(review), attempted=attempted[:2000], observations=tuple(observations),
        findings_omitted=review.findings_omitted, learning_activities=tuple(a[:300] for a in activities[:5]),
        consider_revising=tuple(r[:300] for r in revising[:8]))


def to_package(content: FeedbackContent, feedback_id: str, approved_by: str, released_by: str) -> FeedbackPackage:
    segs = [Segment(order=1, title="What you attempted",
                    narration="Here is what you described, in your own words: " + content.attempted)]
    for o in content.observations:
        where = f" at {o.location}" if o.location else ""
        segs.append(Segment(order=len(segs) + 1, title=f"Observation: {o.category}",
                            narration=(f"Sage, an AI code reviewer, noted a {o.severity} observation{where}: "
                                       f"{o.sage_description} This is an opinion, not a verified fact. The "
                                       f"concept to study is {o.concept}. {o.why_it_matters}")[:2000]))
    segs.append(Segment(order=len(segs) + 1, title="Suggested activities",
                        narration=" ".join(content.learning_activities)[:2000]))
    segs.append(Segment(order=len(segs) + 1, title="What to consider revising",
                        narration=" ".join(content.consider_revising)[:2000]))
    segs.append(Segment(order=len(segs) + 1, title="Please note", narration=" ".join(content.notices)))
    pkg = FeedbackPackage(feedback_id=feedback_id, student_login=content.student_login,
                          repository=content.repository, pull_request=content.pull_request,
                          reviewed_sha=content.reviewed_sha, review_version=content.sage_review_digest[:16],
                          segments=tuple(segs), approved_by=approved_by, released_by=released_by,
                          released_at=_now(), content_digest="0")
    return pkg.model_copy(update={"content_digest": _package_digest(pkg)})


def _texts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts(v)]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _texts(v)]
    return []


class Attestation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    instructional_quality_reviewed: bool
    confidentiality_reviewed: bool


class FeedbackService:
    def __init__(self, directory: Path, audit: AuditLog, roster: frozenset[str],
                 guard: ContentGuard | None = None) -> None:
        self._dir, self._audit, self._roster, self._guard = directory / "private", audit, roster, guard
        self.released = ReleasedFeedbackStore(directory / "released")
        self._dir.mkdir(parents=True, exist_ok=True)

    def _p(self, feedback_id: str, name: str) -> Path:
        if not re.fullmatch(r"FB-[0-9a-f]{12}", feedback_id):
            raise KeyError("unknown feedback")
        return self._dir / feedback_id / name

    def record(self, feedback_id: str) -> FeedbackRecord | None:
        path = self._p(feedback_id, "record.json")
        return FeedbackRecord.model_validate_json(path.read_text("utf-8")) if path.is_file() else None

    def _content(self, feedback_id: str) -> FeedbackContent:
        return FeedbackContent.model_validate_json(self._p(feedback_id, "content.json").read_text("utf-8"))

    def _save(self, r: FeedbackRecord) -> None:
        atomic_write(self._p(r.feedback_id, "record.json"), r.model_dump_json(indent=2))

    def _log(self, event: AuditEvent, actor: str, r: FeedbackRecord, outcome: str = "ok", **detail: object) -> None:
        self._audit.append(event, actor, outcome, artifact_id=r.feedback_id, source_revision=r.reviewed_sha,  # type: ignore[arg-type]
                           key=f"{event.value}:{r.feedback_id}:{detail.pop('k', '')}",
                           detail={"pull_request": r.pull_request, **detail})  # type: ignore[arg-type]

    def _move(self, r: FeedbackRecord, op: str, actor: Actor, to: FeedbackState) -> None:
        r.history.append(Step(at=_now(), op=op, actor=actor.login, to_state=to.value))
        r.state = to
        self._save(r)

    def _deny(self, actor: Actor, op: str, r: FeedbackRecord | None, reason: str, exc: Exception) -> Exception:
        self._audit.append(AuditEvent.FEEDBACK_REFUSED, actor.login, "refused",
                           artifact_id=r.feedback_id if r else None, source_revision=r.reviewed_sha if r else None,
                           detail={"op": op, "reason": reason})
        return exc

    # -- preparation (Archie) ----------------------------------------------------------------
    def prepare(self, snapshot: StudentPRSnapshot, actor: Actor = ARCHIE) -> FeedbackRecord:
        """Draft feedback for this commit. Needs no assignment (it is not curriculum) but needs a
        rostered student and a current, complete, trusted Sage review. Repeating it is a no-op."""
        if not (isinstance(actor, HumanActor) or actor == ARCHIE):
            raise self._deny(actor, "prepare", None, "actor_not_permitted", Unauthorized("not permitted"))
        try:
            content = build_feedback(snapshot, self._roster)
        except NotEligible as exc:
            self._audit.append(AuditEvent.FEEDBACK_REFUSED, actor.login, "refused", source_revision=snapshot.head_sha,
                               detail={"op": "prepare", "reason": "not_eligible", "pull_request": snapshot.number})
            raise exc
        fid = feedback_id_for(content.repository, content.pull_request, content.reviewed_sha)
        with locked(self._dir / f".{fid}.lock"):
            existing = self.record(fid)
            if existing is not None:
                return existing
            digest = _sha(content.model_dump_json())
            atomic_write(self._p(fid, "content.json"), content.model_dump_json(indent=2))
            record = FeedbackRecord(feedback_id=fid, repository=content.repository, pull_request=content.pull_request,
                                    student_login=content.student_login, reviewed_sha=content.reviewed_sha,
                                    sage_review_digest=content.sage_review_digest, content_digest=digest)
            self._save(record)
        self._log(AuditEvent.FEEDBACK_PREPARED, actor.login, record, observations=len(content.observations))
        return record

    def submit(self, feedback_id: str, actor: Actor) -> FeedbackRecord:
        if not (isinstance(actor, HumanActor) or actor == ARCHIE):
            raise self._deny(actor, "submit", None, "actor_not_permitted", Unauthorized("not permitted"))
        with locked(self._dir / f".{feedback_id}.lock"):
            r = self._need(feedback_id)
            if r.state is FeedbackState.REVIEW_PENDING:
                return r
            if r.state is not FeedbackState.DRAFT:
                raise self._deny(actor, "submit", r, f"illegal_from_{r.state.value}", ValueError("illegal transition"))
            r.submitted_by = actor.login
            self._move(r, "submit", actor, FeedbackState.REVIEW_PENDING)
        self._log(AuditEvent.FEEDBACK_SUBMITTED, actor.login, r)
        return r

    def _need(self, feedback_id: str) -> FeedbackRecord:
        r = self.record(feedback_id)
        if r is None:
            raise KeyError("unknown feedback")
        return r

    def approve(self, feedback_id: str, actor: Actor, attestation: Attestation) -> FeedbackRecord:
        with locked(self._dir / f".{feedback_id}.lock"):
            r = self._need(feedback_id)
            if not isinstance(actor, HumanActor) or actor.role not in (Role.FOUNDER, Role.REVIEWER):
                raise self._deny(actor, "approve", r, "human_role_required", Unauthorized("a human reviewer is required"))
            if r.state is FeedbackState.APPROVED and r.approved_by == actor.login:
                return r
            if r.state is not FeedbackState.REVIEW_PENDING:
                raise self._deny(actor, "approve", r, f"illegal_from_{r.state.value}", ValueError("illegal transition"))
            if actor.login == r.submitted_by:
                raise self._deny(actor, "approve", r, "self_approval", Unauthorized("cannot approve your own submission"))
            if not (attestation.instructional_quality_reviewed and attestation.confidentiality_reviewed):
                raise self._deny(actor, "approve", r, "attestation_incomplete", ValueError("attestation incomplete"))
            r.approved_by = actor.login
            self._move(r, "approve", actor, FeedbackState.APPROVED)
        self._log(AuditEvent.FEEDBACK_APPROVED, actor.login, r)
        return r

    def release(self, feedback_id: str, actor: Actor, current_head_sha: str | None) -> FeedbackRecord:
        """Founder only. `current_head_sha` is the PR's live head, fetched by the caller; if it is
        missing or has moved past the reviewed commit the feedback is marked stale and not released."""
        with locked(self._dir / f".{feedback_id}.lock"):
            r = self._need(feedback_id)
            if not isinstance(actor, HumanActor) or actor.role is not Role.FOUNDER:
                raise self._deny(actor, "release", r, "founder_required", Unauthorized("only a founder can release"))
            if r.state is FeedbackState.RELEASED:
                return r
            if r.state is not FeedbackState.APPROVED:
                raise self._deny(actor, "release", r, f"illegal_from_{r.state.value}", ValueError("illegal transition"))
            if current_head_sha != r.reviewed_sha:
                self._stale(r, actor, "head_moved_or_unknown")
                raise self._deny(actor, "release", r, "stale", NotEligible("the pull request changed after the review"))
            content = self._content(feedback_id)
            if _sha(content.model_dump_json()) != r.content_digest:
                raise self._deny(actor, "release", r, "content_modified", ValueError("content changed after approval"))
            package = to_package(content, feedback_id, r.approved_by or "", actor.login)
            scan = self._guard or ContentGuard()
            texts = _texts(package.model_dump(mode="json"))
            if any(scan.check(t) for t in texts):
                raise self._deny(actor, "release", r, "confidentiality", ValueError("confidentiality checks failed"))
            self.released.write(package)
            r.released_by = actor.login
            self._move(r, "release", actor, FeedbackState.RELEASED)
        self._log(AuditEvent.FEEDBACK_RELEASED, actor.login, r)
        return r

    def _stale(self, r: FeedbackRecord, actor: Actor, reason: str) -> None:
        r.stale_reason = reason
        self.released.remove(r.feedback_id)
        self._move(r, "stale", actor, FeedbackState.STALE)
        self._log(AuditEvent.FEEDBACK_STALE, actor.login, r, "refused", reason=reason)

    def observe_head(self, repository: str, pull_request: int, current_head_sha: str,
                     actor: Actor = ARCHIE) -> list[str]:
        """Event hook for a PR update: any live feedback for an older commit becomes stale."""
        marked: list[str] = []
        for path in sorted(self._dir.glob("FB-*/record.json")):
            r = FeedbackRecord.model_validate_json(path.read_text("utf-8"))
            if (r.repository, r.pull_request) != (repository, pull_request) or r.reviewed_sha == current_head_sha:
                continue
            if r.state in (FeedbackState.STALE, FeedbackState.REVOKED):
                continue
            with locked(self._dir / f".{r.feedback_id}.lock"):
                fresh = self._need(r.feedback_id)
                if fresh.state not in (FeedbackState.STALE, FeedbackState.REVOKED):
                    self._stale(fresh, actor, "head_moved")
                    marked.append(fresh.feedback_id)
        return marked

    def revoke(self, feedback_id: str, actor: Actor) -> FeedbackRecord:
        with locked(self._dir / f".{feedback_id}.lock"):
            r = self._need(feedback_id)
            if not isinstance(actor, HumanActor) or actor.role is not Role.FOUNDER:
                raise self._deny(actor, "revoke", r, "founder_required", Unauthorized("only a founder can revoke"))
            if r.state is FeedbackState.REVOKED:
                return r
            self.released.remove(feedback_id)
            self._move(r, "revoke", actor, FeedbackState.REVOKED)
        self._log(AuditEvent.FEEDBACK_REVOKED, actor.login, r)
        return r

    def status(self, feedback_id: str) -> dict[str, object]:
        r = self._need(feedback_id)
        return {"feedback_id": r.feedback_id, "state": r.state.value, "repository": r.repository,
                "pull_request": r.pull_request, "reviewed_sha": r.reviewed_sha,
                "submitted_by": r.submitted_by, "approved_by": r.approved_by, "stale_reason": r.stale_reason}


class ReleasedFeedbackStore:
    """Released feedback packages only. This is all `FeedbackGateway` can reach."""

    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    def _p(self, feedback_id: str) -> Path:
        if not re.fullmatch(r"FB-[0-9a-f]{12}", feedback_id):
            raise KeyError("unknown feedback")
        return self._root / f"{feedback_id}.json"

    def write(self, package: FeedbackPackage) -> None:
        atomic_write(self._p(package.feedback_id), package.model_dump_json(), 0o644)

    def remove(self, feedback_id: str) -> None:
        self._p(feedback_id).unlink(missing_ok=True)

    def read(self, feedback_id: str) -> FeedbackPackage | None:
        path = self._p(feedback_id)
        return FeedbackPackage.model_validate_json(path.read_text("utf-8")) if path.is_file() else None


class FeedbackGateway:
    """What Ada may call for feedback. It holds the released store and nothing else."""

    def __init__(self, released: ReleasedFeedbackStore, audit: AuditLog) -> None:
        self._released, self._audit = released, audit

    def get(self, feedback_id: str, student_login: str) -> FeedbackPackage:
        def no(reason: str) -> LookupError:
            self._audit.append(AuditEvent.FEEDBACK_REFUSED, f"agent:ada", "refused",
                               artifact_id=str(feedback_id)[:40], detail={"op": "retrieve", "reason": reason})
            return LookupError("feedback is not available")

        try:
            package = self._released.read(feedback_id)
        except Exception:
            raise no("unreadable") from None
        if package is None:
            raise no("not_released")
        if not package.verify():
            raise no("integrity")
        if package.student_login != student_login.strip().lower():
            raise no("wrong_student")
        self._audit.append(AuditEvent.FEEDBACK_RETRIEVED, "agent:ada", artifact_id=feedback_id,
                           source_revision=package.reviewed_sha, detail={"pull_request": package.pull_request})
        return package
