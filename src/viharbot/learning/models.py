"""Typed records for the learning pipeline. Source text is data, never instructions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Repo = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")]
Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


class ChangedPath(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    status: str
    additions: int = 0
    deletions: int = 0


class SageFinding(BaseModel):
    """One finding from Sage's advisory comment. An AI opinion, not a verified fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    severity: str  # critical | high | medium | low | info, as Sage labelled it
    category: str
    basis: str  # e.g. "verified observation" or "reasoned interpretation"
    location: str | None = None  # "path:line" as Sage cited it; not re-verified here
    description: str


class SageEvidence(BaseModel):
    """What Sage said about a pull request, with where it came from. Untrusted data."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    reviewed_sha: Sha
    reviewed_head_current: bool | None  # None when the PR head SHA was not retrieved
    completeness: str  # complete | incomplete | failed | skipped | stale
    recommendation: str
    findings: tuple[SageFinding, ...] = ()
    comment_url: str | None = None
    findings_omitted: int = 0  # > 0 means more findings existed than were kept


class GovernanceNote(BaseModel):
    """One deterministic comparison of a pull request against a written rule.

    A `conflict` means the retrieved evidence and a rule point different ways, or the evidence cannot
    show the rule was met. Archie records it and never resolves it: only a human can.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: str  # "observation" | "conflict"
    finding: str
    basis: str  # "verified_in_diff" | "author_statement" | "sage_advisory" | "not_available"
    policy: str  # the written rule compared against, by document and section
    source: str  # where the evidence came from


class PRSnapshot(BaseModel):
    """What was retrieved. Patches are deliberately not kept; only paths and counts are."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    repository: Repo
    number: int = Field(ge=1)
    title: str
    body: str
    merged_at: datetime
    merge_commit_sha: Sha
    base_ref: str
    files: tuple[ChangedPath, ...]
    commit_subjects: tuple[str, ...] = ()
    discussion: tuple[str, ...] = ()  # review and issue comments, untrusted
    software_version: str = "unknown"
    files_omitted: int = 0  # > 0 means the evidence is incomplete
    head_sha: Sha | None = None  # the PR's last commit, used to tell whether a Sage review is current
    sage_review: SageEvidence | None = None  # from a trusted Sage author only; see sage_evidence.py


class StudentPRSnapshot(BaseModel):
    """A pull request as it stands now (open or merged), for student feedback. Read-only; no patches.

    `head_sha` is the exact commit the feedback will be tied to. Text fields are untrusted."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    repository: Repo
    number: int = Field(ge=1)
    author_login: str
    state: str  # "open" | "closed"
    title: str
    body: str
    head_sha: Sha
    base_ref: str
    files: tuple[ChangedPath, ...]
    sage_review: SageEvidence | None = None
    files_omitted: int = 0


class Verification(StrEnum):
    UNVERIFIED = "unverified"  # extracted from one source, no human or second-source check
    EVIDENCE_CHECKED = "evidence_checked"  # every claim maps to a retrieved source
    APPROVED = "approved"  # a human merged the instructor PR; only a human sets this


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    question: str
    answer: str  # "Not stated in the evidence." when nothing supports an answer
    basis: str  # "author_statement" | "verified_in_diff" | "not_available"
    source: str


class LearningArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str
    topic: str
    topics: tuple[str, ...]
    learning_objectives: tuple[str, ...]  # drafts for human refinement
    source_repository: Repo
    pull_request: int
    merge_commit_sha: Sha
    source_references: tuple[str, ...]
    extracted_on: str
    software_version: str
    verification: Verification
    prerequisites: tuple[str, ...]
    curriculum_placement: str
    claims: tuple[Claim, ...]
    evidence_complete: bool
    discussion_excerpts: tuple[str, ...] = ()
    sage_review: SageEvidence | None = None
    governance_notes: tuple[GovernanceNote, ...] = ()


class IngestState(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    PROPOSED = "proposed"
    FAILED = "failed"


class IngestionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_key: str
    repository: Repo
    pull_request: int
    merge_commit_sha: Sha
    state: IngestState
    attempts: int = 0
    assignment_id: str | None = None  # the founder assignment that authorised this work
    artifact_id: str | None = None
    proposal_url: str | None = None
    error: str | None = None  # an exception class name only, never a message or response body
    updated_at: str
