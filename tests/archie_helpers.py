"""Shared fixtures for the Archie workflow tests (epic #139). Offline: fake GitHub, fake model."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from test_archie_sage_evidence import comment as sage_comment
from test_learning_pipeline import INS, SRC, FakeGitHub
from tests.sage_review_helpers import HEAD
from viharbot.learning.ada import AdaGateway, PublishedStore
from viharbot.learning.assignment import Assignment, AssignmentStore, EvidenceScope
from viharbot.learning.audit import AuditLog
from viharbot.learning.authn import CredentialAuthenticator, HumanActor, hash_token
from viharbot.learning.confidentiality import ContentGuard
from viharbot.learning.evidence import EvidenceBundle, collect_evidence
from viharbot.learning.github import SourceReader
from viharbot.learning.publication import Attestation, DraftStore, PublicationService
from viharbot.learning.synthesis import build_baseline
from viharbot.sage_review_github import GitHubClient

FOUNDER_TOKEN = "founder-token-" + "f" * 24
REVIEWER_TOKEN = "reviewer-token-" + "r" * 24
OTHER_TOKEN = "second-reviewer-" + "s" * 24
FULL_ATTESTATION = Attestation(instructional_quality_reviewed=True, confidentiality_reviewed=True)


def authenticator() -> CredentialAuthenticator:
    return CredentialAuthenticator(
        {"vchokshi": hash_token(FOUNDER_TOKEN)},
        {"reviewer-one": hash_token(REVIEWER_TOKEN), "reviewer-two": hash_token(OTHER_TOKEN)})


@dataclass
class Env:
    root: Path
    fake: FakeGitHub
    auth: CredentialAuthenticator
    founder: HumanActor
    reviewer: HumanActor
    reviewer2: HumanActor
    audit: AuditLog
    assignments: AssignmentStore
    drafts: DraftStore
    published: PublishedStore
    service: PublicationService
    gateway: AdaGateway
    reader: SourceReader
    guard: ContentGuard

    def assign(self, prs=(7,), **kw) -> Assignment:
        return self.assignments.assign(self.founder, "agent identity", SRC, prs, **kw)

    def bundle(self, assignment: Assignment, **kw) -> EvidenceBundle:
        return collect_evidence(assignment.assignment_id, 7, reader=self.reader, assignments=self.assignments,
                                audit=self.audit, **kw)

    def draft(self, assignment: Assignment, **kw):
        return build_baseline(self.bundle(assignment, **kw))


def make_env(tmp_path: Path, with_sage: bool = True, guard: ContentGuard | None = None) -> Env:
    fake = FakeGitHub(comments=[sage_comment()] if with_sage else [])
    if with_sage:
        fake.head_sha = HEAD
    auth = authenticator()
    audit = AuditLog(tmp_path / "audit")
    assignments = AssignmentStore(tmp_path / "assignments", frozenset({"vchokshi"}),
                                  require_authenticated=True, audit=audit)
    drafts, published = DraftStore(tmp_path / "drafts"), PublishedStore(tmp_path / "published")
    guard = guard if guard is not None else ContentGuard()
    return Env(
        root=tmp_path, fake=fake, auth=auth,
        founder=auth.authenticate("vchokshi", FOUNDER_TOKEN),
        reviewer=auth.authenticate("reviewer-one", REVIEWER_TOKEN),
        reviewer2=auth.authenticate("reviewer-two", OTHER_TOKEN),
        audit=audit, assignments=assignments, drafts=drafts, published=published,
        service=PublicationService(drafts, published, assignments, audit, guard),
        gateway=AdaGateway(published, audit),
        reader=SourceReader(GitHubClient("tok", fake), SRC), guard=guard)


def make_workflow(env: Env, **kw):
    """A `CurriculumWorkflow` over the same stores as `env`."""
    from viharbot.learning.workflow import CurriculumWorkflow
    return CurriculumWorkflow(env.root / "workflows", assignments=env.assignments, service=env.service,
                              audit=env.audit, reader=env.reader, guard=env.guard, **kw)
