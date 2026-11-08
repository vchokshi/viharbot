"""The trainee side of the boundary: Ada's delivery package, the published store and Ada's gateway
(issues #144, #151).

This module imports nothing that can reach instructor material: not the draft store, the assignment
store, the instructor reader or the evidence code (a test checks its imports). `AdaGateway` holds one
thing, a `PublishedStore`, which only ever contains `AdaPackage` files built from `TraineeContent`.
So Ada cannot read a draft, an expected answer or an instructor credential: none is reachable from
here.

Ada is a scripted presenter. The package fixes the segments and the prompts she may present;
`delivery_mode` is "scripted" and there is no field through which she could be handed free-form tutoring
authority. Retrieval fails closed: only a package whose state is `published`, whose integrity digest
still matches and which has not been revoked is returned.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ..curriculum.models import LessonId
from .audit import AuditEvent, AuditLog
from .fsutil import atomic_write, locked
from .lesson import Confidence, SourceRef, Statement, TraineeContent, digest_of
from .basis import Basis
from .models import Repo, Sha

PACKAGE_SCHEMA = "1.0"
Narration = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4500)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class NotAvailable(LookupError):
    """The requested lesson is not published, was revoked, or failed its integrity check."""


class _F(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Segment(_F):
    order: int = Field(ge=1)
    kind: Literal["intro", "concept", "architecture", "alternative", "example", "governance", "exercise",
                  "checkpoint", "recap"]
    title: Label
    narration: Narration  # the script Ada speaks; also the transcript
    basis_label: str | None = None  # how well known the content is, spoken to the learner


class ApprovedPrompt(_F):
    prompt_id: Annotated[str, StringConstraints(pattern=r"^c[0-9]{1,2}$")]
    kind: Literal["recall", "explain", "apply", "reflect"]
    text: Label
    hint: Label | None = None


class NavItem(_F):
    order: int = Field(ge=1)
    label: Label


class AccessibilityMeta(_F):
    captions_required: Literal[True] = True
    transcript_included: Literal[True] = True
    notes: tuple[Label, ...]


class Provenance(_F):
    source_repository: Repo
    pull_request: int
    source_sha: Sha
    artifact_version: str
    review_refs: tuple[Label, ...]
    evidence_confidence: Confidence
    approved_by: str
    approved_at: str
    approval_revision: int
    quality_reviewed: Literal[True] = True
    confidentiality_reviewed: Literal[True] = True
    published_by: str
    published_at: str


class Integrity(_F):
    algorithm: Literal["sha256"] = "sha256"
    schema_version: Literal["1.0"] = PACKAGE_SCHEMA
    trainee_digest: str  # digest of the TraineeContent this was built from
    content_digest: str  # digest of everything above except provenance and integrity


class PackageContent(_F):
    lesson_id: LessonId
    version: int = Field(ge=1)
    title: Label
    segments: tuple[Segment, ...] = Field(min_length=1)
    prompts: tuple[ApprovedPrompt, ...] = ()
    navigation: tuple[NavItem, ...]
    accessibility: AccessibilityMeta
    references: tuple[SourceRef, ...]
    delivery_mode: Literal["scripted"] = "scripted"


class AdaPackage(_F):
    schema_version: Literal["1.0"] = PACKAGE_SCHEMA
    content: PackageContent
    provenance: Provenance
    integrity: Integrity

    def verify(self) -> bool:
        return self.integrity.content_digest == digest_of(self.content)


_LEAD = {Basis.AUTHOR_STATEMENT: "The pull request author wrote",
         Basis.VERIFIED_OBSERVATION: "A fact retrieved from the repository",
         Basis.SAGE_ADVISORY: "Sage, an AI reviewer, advised. This is an opinion, not a verified fact",
         Basis.KEPLER_HANDOFF: "Kepler's engineering notes record",
         Basis.UNSUPPORTED: "This is not supported by the evidence"}


def _statement_segments(kind: str, statements: tuple[Statement, ...], start: int, heading: str) -> list[Segment]:
    return [Segment(order=start + i + 1, kind=kind, title=f"{heading} {i + 1}",  # type: ignore[arg-type]
                    narration=f"{_LEAD[s.basis]}: {s.text}", basis_label=s.basis.value)
            for i, s in enumerate(statements)]


def build_content(lesson_id: str, version: int, t: TraineeContent) -> PackageContent:
    segs: list[Segment] = [Segment(
        order=1, kind="intro", title=t.title,
        narration=(f"Welcome. This lesson is for {t.audience} By the end you should be able to: "
                   + " ".join(f"{i + 1}. {o.text}" for i, o in enumerate(t.objectives))))]

    def extend(new: list[Segment]) -> None:
        segs.extend(s.model_copy(update={"order": len(segs) + i + 1}) for i, s in enumerate(new))

    extend(_statement_segments("concept", t.problem_context, 0, "The problem"))
    extend(_statement_segments("architecture", t.architecture, 0, "How it was built"))
    extend([Segment(order=1, kind="alternative", title=a.option, narration=a.trade_off) for a in t.alternatives])
    extend([Segment(order=1, kind="example", title=w.title, narration=w.walkthrough) for w in t.worked_examples])
    extend(_statement_segments("governance", t.security_operational_governance, 0, "Security and governance"))
    extend([Segment(order=1, kind="exercise", title=e.title, narration=e.instructions) for e in t.exercises])
    extend([Segment(order=1, kind="checkpoint", title=f"Check {c.check_id}", narration=c.prompt) for c in t.checks])
    recap = list(t.limitations) + [f"Open question: {q}" for q in t.open_questions]
    if recap:
        extend([Segment(order=1, kind="recap", title="What we do not know yet", narration=" ".join(recap)[:4500])])
    return PackageContent(
        lesson_id=lesson_id, version=version, title=t.title, segments=tuple(segs),
        prompts=tuple(ApprovedPrompt(prompt_id=c.check_id, kind=c.kind, text=c.prompt, hint=c.hint)
                      for c in t.checks),
        navigation=tuple(NavItem(order=s.order, label=s.title) for s in segs),
        accessibility=AccessibilityMeta(notes=t.accessibility), references=t.source_references)


def build_package(lesson_id: str, version: int, trainee: TraineeContent, provenance: Provenance) -> AdaPackage:
    content = build_content(lesson_id, version, trainee)
    return AdaPackage(content=content, provenance=provenance,
                      integrity=Integrity(trainee_digest=digest_of(trainee), content_digest=digest_of(content)))


def package_texts(package: AdaPackage) -> list[str]:
    """Every string in the content Ada could speak, for the publication-time confidentiality scan."""
    out: list[str] = []

    def walk(v: object) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    walk(package.content.model_dump(mode="json"))
    return out


class PublishedStore:
    """`<root>/<lesson_id>/v<N>/package.json` plus `state.json`. Holds trainee-safe packages only."""

    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    def _dir(self, lesson_id: str, version: int) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", lesson_id) or version < 1:
            raise NotAvailable("not available")
        return self._root / lesson_id / f"v{version}"

    def publish(self, package: AdaPackage) -> None:
        d = self._dir(package.content.lesson_id, package.content.version)
        with locked(d.parent / ".lock"):
            atomic_write(d / "package.json", package.model_dump_json(), 0o644)
            atomic_write(d / "state.json", json.dumps({"state": "published",
                         "at": datetime.now(timezone.utc).isoformat()}), 0o644)

    def revoke(self, lesson_id: str, version: int) -> None:
        d = self._dir(lesson_id, version)
        with locked(d.parent / ".lock"):
            atomic_write(d / "state.json", json.dumps({"state": "revoked",
                         "at": datetime.now(timezone.utc).isoformat()}), 0o644)  # first: stops retrieval
            (d / "package.json").unlink(missing_ok=True)

    def state(self, lesson_id: str, version: int) -> str:
        try:
            return str(json.loads((self._dir(lesson_id, version) / "state.json").read_text("utf-8"))["state"])
        except (OSError, ValueError, KeyError):
            return "absent"

    def read(self, lesson_id: str, version: int) -> AdaPackage | None:
        path = self._dir(lesson_id, version) / "package.json"
        return AdaPackage.model_validate_json(path.read_text("utf-8")) if path.is_file() else None

    def versions(self, lesson_id: str) -> list[int]:
        base = self._dir(lesson_id, 1).parent
        return sorted(int(p.name[1:]) for p in base.glob("v*") if p.name[1:].isdigit()) if base.is_dir() else []


class AdaGateway:
    """What Ada may call. It can reach only the published store."""

    def __init__(self, published: PublishedStore, audit: AuditLog) -> None:
        self._published = published
        self._audit = audit

    def get(self, lesson_id: str, version: int | None = None, requester: str = "agent:ada") -> AdaPackage:
        def refuse(reason: str) -> NotAvailable:
            self._audit.append(AuditEvent.ADA_REFUSED, requester, "refused", artifact_id=str(lesson_id)[:80],
                               detail={"reason": reason, "version": version or 0})
            return NotAvailable("that lesson is not available")

        if not isinstance(lesson_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", lesson_id):
            raise refuse("invalid_id")
        try:
            wanted = version if version is not None else next(
                (v for v in reversed(self._published.versions(lesson_id))
                 if self._published.state(lesson_id, v) == "published"), None)
            if wanted is None:
                raise refuse("no_published_version")
            if self._published.state(lesson_id, wanted) != "published":
                raise refuse("not_published")
            package = self._published.read(lesson_id, wanted)
        except NotAvailable:
            raise
        except Exception:
            raise refuse("unreadable") from None
        if package is None or not package.verify():
            raise refuse("integrity")
        self._audit.append(AuditEvent.ADA_RETRIEVED, requester, artifact_id=lesson_id,
                           source_revision=package.provenance.source_sha,
                           detail={"version": wanted, "digest": package.integrity.content_digest[:16]})
        return package
