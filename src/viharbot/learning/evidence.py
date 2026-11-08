"""Assignment-scoped evidence for one pull request (epic #139, issues #141, #142).

`collect_evidence` is the only place the workflow gathers evidence. It refuses unless an active
founder assignment covers the exact repository and pull request, and it uses only the evidence kinds
that assignment permits. Each item is typed by how well it is known (`Basis`):

    verified_observation  a retrieved fact: a changed path, a count
    author_statement      what the pull request author wrote; true only as "the author said so"
    sage_advisory         Sage's AI opinion, admitted only if trusted, complete and current
    kepler_handoff        an engineering artifact from Kepler (adapter contract; see below)
    instructor_guidance   read from the restricted repository; kept apart, never in trainee content
    unsupported           a question the evidence does not answer

All repository text is untrusted data. Suspicious phrasing is flagged on the item but never
interpreted: no decision in this package reads evidence text. Instructor text lives only in memory
(`InstructorEvidence.text` is excluded from serialisation), so persisting a bundle cannot leak it.

Kepler: the handoff format is issue #130, which is not implemented. `KeplerHandoff` and
`KeplerHandoffProvider` are the contract an adapter must meet; the default provider reports
"unavailable" and nothing is invented. A handoff is accepted only if it names this repository, this
pull request and this exact source commit.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..sage_review_github import GitHubError
from .assignment import AssignmentStore, EvidenceScope, NotAssigned
from .audit import AuditEvent, AuditLog
from .basis import Basis
from .extract import extract
from .github import SourceReader
from .instructor_access import AccessDenied, InstructorReader, path_hash
from .models import LearningArtifact, Repo, Sha

MAX_ITEM_CHARS = 2000
MAX_HANDOFF_ITEMS = 20
_INJECTION = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above) (?:instructions|rules)|disregard (?:the )?(?:above|previous)|"
    r"system prompt|you are now|new instructions|reveal (?:the )?(?:answer|rubric|key)|"
    r"(?:approve|publish|merge|release) (?:this|the) (?:lesson|pr|pull request|package)|"
    r"as the founder|act as|<\|[a-z_]+\|>|\[\[?system", re.I)


class _F(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceItem(_F):
    evidence_id: str
    basis: Basis
    source: str
    source_sha: Sha | None = None
    version: str | None = None
    text: str = Field(max_length=MAX_ITEM_CHARS)
    flags: tuple[str, ...] = ()


class InstructorEvidence(_F):
    """A reference to instructor material. The text is held in memory only and never serialised."""

    evidence_id: str
    path_hash: str
    blob_sha: str
    text: str = Field(default="", exclude=True, repr=False)


class KeplerHandoff(_F):
    """Contract for a Kepler engineering handoff (issue #130). Not an existing artifact format."""

    format_version: Literal["1"]
    source_repository: Repo
    source_pull_request: int = Field(ge=1)
    source_sha: Sha
    artifact_version: str = Field(min_length=1, max_length=40)
    location_url: str = Field(min_length=1, max_length=300)
    design_decisions: tuple[str, ...] = Field(default=(), max_length=MAX_HANDOFF_ITEMS)
    api_contracts: tuple[str, ...] = Field(default=(), max_length=MAX_HANDOFF_ITEMS)
    tests_and_results: tuple[str, ...] = Field(default=(), max_length=MAX_HANDOFF_ITEMS)
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_HANDOFF_ITEMS)
    learning_notes: tuple[str, ...] = Field(default=(), max_length=MAX_HANDOFF_ITEMS)
    open_questions: tuple[str, ...] = Field(default=(), max_length=MAX_HANDOFF_ITEMS)


class KeplerHandoffProvider(Protocol):
    def fetch(self, repository: str, pull_request: int) -> KeplerHandoff | None: ...


class UnavailableKeplerProvider:
    """The default: issue #130 is not built, so there is nothing to fetch."""

    def fetch(self, repository: str, pull_request: int) -> KeplerHandoff | None:
        return None


class EvidenceBundle(_F):
    assignment_id: str
    workflow_id: str
    repository: Repo
    pull_request: int
    source_sha: Sha
    collected_at: str
    sage_status: Literal["current", "stale", "incomplete", "absent", "not_permitted"]
    kepler_status: Literal["included", "unavailable", "not_permitted", "mismatch"]
    instructor_status: Literal["consulted", "not_permitted", "reader_unavailable", "partial"]
    items: tuple[EvidenceItem, ...]
    instructor: tuple[InstructorEvidence, ...] = ()
    governance_conflicts: int = 0
    limitations: tuple[str, ...] = ()
    artifact: LearningArtifact

    def ids(self) -> frozenset[str]:
        return frozenset(i.evidence_id for i in self.items)

    def item(self, evidence_id: str) -> EvidenceItem | None:
        return next((i for i in self.items if i.evidence_id == evidence_id), None)

    def digest(self) -> str:
        """Stable over the trainee-visible evidence; ignores timestamps and instructor text."""
        body = [(i.evidence_id, i.basis.value, i.source_sha) for i in self.items]
        body += [(i.evidence_id, i.blob_sha) for i in self.instructor]
        return hashlib.sha256(repr((self.source_sha, self.sage_status, body)).encode()).hexdigest()


def workflow_id_for(assignment_id: str, repository: str, pull_request: int, source_sha: str) -> str:
    raw = f"{assignment_id}|{repository}|{pull_request}|{source_sha}"
    return "WF-" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def _item(basis: Basis, source: str, text: str, sha: str | None = None, version: str | None = None) -> EvidenceItem:
    text = text.strip()[:MAX_ITEM_CHARS]
    ident = "E-" + hashlib.sha256(f"{basis.value}|{source}|{text}".encode()).hexdigest()[:10]
    flags = ("possible_prompt_injection",) if _INJECTION.search(text) else ()
    return EvidenceItem(evidence_id=ident, basis=basis, source=source[:200], source_sha=sha,
                        version=version, text=text, flags=flags)


_CLAIM_BASIS = {"author_statement": Basis.AUTHOR_STATEMENT, "verified_in_diff": Basis.VERIFIED_OBSERVATION,
                "not_available": Basis.UNSUPPORTED}
_NOTE_BASIS = {**_CLAIM_BASIS, "sage_advisory": Basis.SAGE_ADVISORY}


def collect_evidence(
    assignment_id: str, pull_request: int, *, reader: SourceReader, assignments: AssignmentStore,
    audit: AuditLog, kepler: KeplerHandoffProvider | None = None,
    instructor: InstructorReader | None = None, actor: str = "agent:archie",
) -> EvidenceBundle:
    assignment = assignments.require_active(assignment_id)  # unknown, cancelled or completed: refused
    if not assignment.covers(reader.repository, pull_request) or not assignment.permits(
            EvidenceScope.SOURCE_REVISION):
        audit.append(AuditEvent.REJECTED, actor, "refused", assignment_id=assignment_id,
                     detail={"reason": "scope_mismatch", "pull_request": pull_request})
        raise NotAssigned("this assignment does not cover that repository and pull request")
    snapshot = reader.snapshot(pull_request)
    artifact = extract(snapshot)
    wf = workflow_id_for(assignment_id, snapshot.repository, pull_request, snapshot.merge_commit_sha)
    audit.append(AuditEvent.EVIDENCE_RETRIEVED, actor, assignment_id=assignment_id, workflow_id=wf,
                 artifact_id=artifact.artifact_id, source_revision=snapshot.merge_commit_sha,
                 key=f"retrieved:{wf}",
                 detail={"files": len(snapshot.files), "files_omitted": snapshot.files_omitted,
                         "software_version": snapshot.software_version})
    src = f"{snapshot.repository}#{pull_request}"
    sha = snapshot.merge_commit_sha
    items: list[EvidenceItem] = []
    limits: list[str] = []
    for claim in artifact.claims:
        basis = _CLAIM_BASIS[claim.basis]
        text = claim.question if basis is Basis.UNSUPPORTED else f"{claim.question} {claim.answer}"
        items.append(_item(basis, claim.source, text, sha, artifact.software_version))
    if snapshot.files_omitted:
        limits.append(f"{snapshot.files_omitted} changed files were beyond the retrieval limit.")

    # Sage: admitted only when trusted (parser), complete and reviewing the PR's final head.
    sage = snapshot.sage_review
    if not assignment.permits(EvidenceScope.SAGE_REVIEW):
        sage_status = "not_permitted"
    elif sage is None:
        sage_status = "absent"
        limits.append("No trusted Sage review was found; no Sage findings are used.")
    elif sage.completeness != "complete":
        sage_status = "incomplete"
        limits.append("Sage's review was not complete; its findings are not used.")
    elif sage.reviewed_head_current is not True:
        sage_status = "stale"
        limits.append("Sage's review does not match the pull request's final head (or currency is "
                      "unknown); its findings are not used.")
    else:
        sage_status = "current"
        for finding in sage.findings:
            where = f" at {finding.location}" if finding.location else ""
            items.append(_item(Basis.SAGE_ADVISORY, sage.comment_url or src,
                               f"[{finding.severity}] {finding.category}{where}: {finding.description}",
                               sage.reviewed_sha))
    audit.append(AuditEvent.EVIDENCE_SAGE, actor, assignment_id=assignment_id, workflow_id=wf,
                 artifact_id=artifact.artifact_id, source_revision=sha, key=f"sage:{wf}:{sage_status}",
                 detail={"sage_status": sage_status,
                         "reviewed_sha": sage.reviewed_sha[:12] if sage else "none",
                         "recommendation": sage.recommendation[:40] if sage else "none"})

    conflicts = 0
    if assignment.permits(EvidenceScope.GOVERNANCE):
        for note in artifact.governance_notes:
            if note.basis == "sage_advisory" and sage_status != "current":
                continue  # restates a Sage opinion from a review that is stale, incomplete or not permitted
            conflicts += note.status == "conflict"
            items.append(_item(_NOTE_BASIS.get(note.basis, Basis.UNSUPPORTED), f"{note.source} ({note.policy})",
                               f"{note.status}: {note.finding}", sha))

    kepler_status = "not_permitted"
    if assignment.permits(EvidenceScope.KEPLER_HANDOFF):
        handoff = (kepler or UnavailableKeplerProvider()).fetch(snapshot.repository, pull_request)
        if handoff is None:
            kepler_status = "unavailable"
            limits.append("No Kepler handoff is available (issue #130); none is assumed.")
        elif (handoff.source_repository, handoff.source_pull_request, handoff.source_sha) != (
                snapshot.repository, pull_request, sha):
            kepler_status = "mismatch"
            limits.append("A Kepler handoff did not match this pull request and commit; it was ignored.")
        else:
            kepler_status = "included"
            groups = (("design decision", handoff.design_decisions), ("API contract", handoff.api_contracts),
                      ("tests and results", handoff.tests_and_results), ("limitation", handoff.limitations),
                      ("learning note", handoff.learning_notes), ("open question", handoff.open_questions))
            for label, values in groups:
                for value in values:
                    items.append(_item(Basis.KEPLER_HANDOFF, handoff.location_url, f"Kepler {label}: {value}",
                                       handoff.source_sha, handoff.artifact_version))
        audit.append(AuditEvent.EVIDENCE_KEPLER, actor,
                     "ok" if kepler_status == "included" else "refused", assignment_id=assignment_id,
                     workflow_id=wf, source_revision=sha, key=f"kepler:{wf}:{kepler_status}",
                     detail={"kepler_status": kepler_status})

    instructor_docs: list[InstructorEvidence] = []
    instructor_status = "not_permitted"
    if assignment.permits(EvidenceScope.INSTRUCTOR_GUIDANCE):
        if instructor is None:
            instructor_status = "reader_unavailable"
            limits.append("Instructor guidance was granted but no reader is configured.")
        else:
            denied = 0
            for path in assignment.instructor_paths:
                outcome = "ok"
                try:
                    doc = instructor.read(assignment_id, path, request_id=wf)
                    instructor_docs.append(InstructorEvidence(
                        evidence_id="E-" + hashlib.sha256(f"instr|{path_hash(path)}|{doc.blob_sha}".encode()
                                                          ).hexdigest()[:10],
                        path_hash=path_hash(path), blob_sha=doc.blob_sha, text=doc.text))
                except (AccessDenied, GitHubError, NotAssigned, UnicodeDecodeError) as exc:
                    outcome, denied = type(exc).__name__, denied + 1
                audit.append(AuditEvent.INSTRUCTOR_ACCESS, actor, "ok" if outcome == "ok" else "refused",
                             assignment_id=assignment_id, workflow_id=wf, source_revision=sha,
                             key=f"instr:{wf}:{path_hash(path)}:{outcome}",
                             detail={"path_hash": path_hash(path), "result": outcome})
            instructor_status = "partial" if denied else "consulted"
            if denied:
                limits.append(f"{denied} instructor path(s) could not be read; guidance is incomplete.")

    return EvidenceBundle(
        assignment_id=assignment_id, workflow_id=wf, repository=snapshot.repository,
        pull_request=pull_request, source_sha=sha, collected_at=datetime.now(timezone.utc).isoformat(),
        sage_status=sage_status, kepler_status=kepler_status, instructor_status=instructor_status,
        items=tuple(items), instructor=tuple(instructor_docs), governance_conflicts=conflicts,
        limitations=tuple(limits), artifact=artifact)
