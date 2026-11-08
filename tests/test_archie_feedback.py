"""Epic #139, issue #154 (and the delivery contract for #155): student feedback after a verified Sage
review. Offline."""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

import pytest

from archie_helpers import HEAD, SRC, authenticator, FOUNDER_TOKEN, REVIEWER_TOKEN, OTHER_TOKEN
from test_archie_sage_evidence import comment, sage_body
from test_instructor_access import KEY_LINE
from test_learning_pipeline import FakeGitHub
from tests.sage_review_helpers import finding
from viharbot.learning import feedback as fb
from viharbot.learning.audit import AuditLog
from viharbot.learning.authn import ARCHIE, AgentActor, Unauthorized
from viharbot.learning.confidentiality import ContentGuard
from viharbot.learning.feedback import (
    Attestation, FeedbackGateway, FeedbackService, FeedbackState, NotEligible, build_feedback, feedback_id_for,
    roster_from_env,
)
from viharbot.learning.github import SourceReader
from viharbot.sage_review_github import GitHubClient

OTHER_HEAD = "e" * 40
OK = Attestation(instructional_quality_reviewed=True, confidentiality_reviewed=True)


class StudentFake(FakeGitHub):
    """FakeGitHub plus the pull request's author and open/closed state."""

    author = "student-one"

    def __call__(self, method, url, headers, data):
        status, body = super().__call__(method, url, headers, data)
        if method == "GET" and url.endswith(f"/repos/{SRC}/pulls/7"):
            obj = json.loads(body)
            obj.update({"user": {"login": self.author}, "state": "open"})
            return status, json.dumps(obj).encode()
        return status, body


class Setup:
    def __init__(self, tmp_path, comments=None, head=HEAD, roster=("student-one",), guard=None):
        self.fake = StudentFake(comments=[comment()] if comments is None else comments)
        self.fake.head_sha = head
        self.reader = SourceReader(GitHubClient("tok", self.fake), SRC)
        auth = authenticator()
        self.founder = auth.authenticate("vchokshi", FOUNDER_TOKEN)
        self.reviewer = auth.authenticate("reviewer-one", REVIEWER_TOKEN)
        self.reviewer2 = auth.authenticate("reviewer-two", OTHER_TOKEN)
        self.audit = AuditLog(tmp_path / "audit")
        self.root = tmp_path / "feedback"
        self.service = FeedbackService(self.root, self.audit, frozenset(roster), guard)
        self.gateway = FeedbackGateway(self.service.released, self.audit)

    def snapshot(self):
        return self.reader.review_snapshot(7)

    def prepared(self):
        return self.service.prepare(self.snapshot())

    def approved(self):
        r = self.prepared()
        self.service.submit(r.feedback_id, ARCHIE)
        self.service.approve(r.feedback_id, self.reviewer, OK)
        return r

    def released(self):
        r = self.approved()
        self.service.release(r.feedback_id, self.founder, HEAD)
        return r


@pytest.fixture
def s(tmp_path):
    return Setup(tmp_path)


# -- eligibility ----------------------------------------------------------------------------------

def test_valid_sage_review_of_the_current_head_yields_student_safe_feedback(s):
    snap = s.snapshot()
    assert snap.head_sha == HEAD and snap.author_login == "student-one" and snap.state == "open"
    c = build_feedback(snap, frozenset({"student-one"}))
    assert c.reviewed_sha == HEAD and c.attempted_basis == "author_statement"
    assert len(c.observations) == 2 and all(o.basis == "sage_advisory" for o in c.observations)
    assert c.sage_review_ref.startswith("https://") and c.learning_activities and c.consider_revising
    assert any("advisory opinion" in n for n in c.notices) and any("not determined" in n for n in c.notices)


def test_feedback_never_claims_a_finding_was_resolved(s):
    c = build_feedback(s.snapshot(), frozenset({"student-one"}))
    generated = [c.notices, c.learning_activities, c.consider_revising,
                 [o.why_it_matters for o in c.observations], [o.concept for o in c.observations]]
    text = " ".join(t for group in generated for t in group)
    assert not fb._RESOLVED.search(text)
    pkg = fb.to_package(c, "FB-000000000000", "a", "b")
    assert "opinion, not a verified fact" in " ".join(seg.narration for seg in pkg.segments)


def test_author_not_on_the_roster_gets_no_feedback_and_the_refusal_is_audited(tmp_path):
    s = Setup(tmp_path, roster=("someone-else",))
    with pytest.raises(NotEligible):
        s.prepared()
    assert s.audit.records(event="feedback.refused")[0].detail["reason"] == "not_eligible"
    assert not list((s.root / "private").glob("FB-*"))


def test_an_empty_roster_means_nobody_is_eligible(tmp_path):
    with pytest.raises(NotEligible):
        Setup(tmp_path, roster=()).prepared()
    assert roster_from_env("a-b, bad login!, C") == frozenset({"a-b", "c"})


@pytest.mark.parametrize("comments", [
    [],  # no review at all
    [comment(user={"login": "mallory", "type": "User"})],  # forged markers
    [comment(user={"login": "github-actions[bot]", "type": "User"})],
])
def test_no_trusted_sage_review_means_no_feedback(tmp_path, comments):
    with pytest.raises(NotEligible, match="no trusted Sage review"):
        Setup(tmp_path, comments=comments).prepared()


def test_incomplete_sage_review_is_not_enough(tmp_path):
    body = sage_body().replace("<!-- sage-status:complete -->", "<!-- sage-status:failed -->")
    with pytest.raises(NotEligible, match="not complete"):
        Setup(tmp_path, comments=[comment(body)]).prepared()


def test_a_review_of_an_older_commit_is_stale_and_gets_no_feedback(tmp_path):
    with pytest.raises(NotEligible, match="current head"):
        Setup(tmp_path, head=OTHER_HEAD).prepared()  # Sage reviewed HEAD; the PR is now at OTHER_HEAD


def test_findings_come_only_from_sage_nothing_is_invented(tmp_path):
    body = sage_body(findings=[])
    s = Setup(tmp_path, comments=[comment(body)])
    c = build_feedback(s.snapshot(), frozenset({"student-one"}))
    assert c.observations == () and "no findings" in " ".join(c.consider_revising).lower()


def test_concepts_are_mapped_deterministically_from_sage_categories(tmp_path):
    body = sage_body(findings=[finding(severity="high", category="security", description="Input is trusted.")])
    c = build_feedback(Setup(tmp_path, comments=[comment(body)]).snapshot(), frozenset({"student-one"}))
    assert c.observations[0].concept == "Trust boundaries and untrusted input"


def test_feedback_id_is_bound_to_the_reviewed_commit():
    assert feedback_id_for(SRC, 7, HEAD) != feedback_id_for(SRC, 7, OTHER_HEAD)
    assert feedback_id_for(SRC, 7, HEAD) == feedback_id_for(SRC, 7, HEAD)


# -- lifecycle, authorization, release --------------------------------------------------------------

def test_happy_path_prepare_submit_approve_release_and_student_retrieval(s):
    r = s.released()
    assert s.service.status(r.feedback_id)["state"] == "released"
    pkg = s.gateway.get(r.feedback_id, "Student-One")  # login is case-insensitive
    assert pkg.verify() and pkg.reviewed_sha == HEAD and pkg.delivery_mode == "scripted"
    assert pkg.approved_by == "reviewer-one" and pkg.released_by == "vchokshi"
    assert pkg.review_version == s.service._content(r.feedback_id).sage_review_digest[:16]
    assert s.audit.verify()


def test_nothing_is_retrievable_before_release(s):
    r = s.approved()
    with pytest.raises(LookupError):
        s.gateway.get(r.feedback_id, "student-one")


def test_only_the_student_it_was_written_for_can_retrieve_it(s):
    r = s.released()
    with pytest.raises(LookupError):
        s.gateway.get(r.feedback_id, "student-two")
    assert s.audit.records(event="feedback.refused")[-1].detail["reason"] == "wrong_student"


@pytest.mark.parametrize("who", ["reviewer", "reviewer2", "agent_archie", "agent_ada"])
def test_only_a_founder_can_release(s, who):
    r = s.approved()
    actor = {"reviewer": s.reviewer, "reviewer2": s.reviewer2, "agent_archie": ARCHIE,
             "agent_ada": AgentActor("ada")}[who]
    with pytest.raises(Unauthorized):
        s.service.release(r.feedback_id, actor, HEAD)
    assert s.service.status(r.feedback_id)["state"] == "approved"


def test_agents_cannot_approve_and_the_submitter_cannot_approve_themselves(s):
    r = s.prepared()
    s.service.submit(r.feedback_id, s.founder)
    for actor in (ARCHIE, AgentActor("ada")):
        with pytest.raises(Unauthorized):
            s.service.approve(r.feedback_id, actor, OK)
    with pytest.raises(Unauthorized):
        s.service.approve(r.feedback_id, s.founder, OK)  # submitted by the same human
    assert s.service.approve(r.feedback_id, s.reviewer, OK).state is FeedbackState.APPROVED


def test_approval_needs_the_full_attestation_and_the_right_state(s):
    r = s.prepared()
    with pytest.raises(ValueError):  # approving a draft that was never submitted
        s.service.approve(r.feedback_id, s.reviewer, OK)
    s.service.submit(r.feedback_id, ARCHIE)
    with pytest.raises(ValueError):
        s.service.approve(r.feedback_id, s.reviewer,
                          Attestation(instructional_quality_reviewed=True, confidentiality_reviewed=False))


def test_release_cannot_skip_review(s):
    r = s.prepared()
    with pytest.raises(ValueError):
        s.service.release(r.feedback_id, s.founder, HEAD)
    s.service.submit(r.feedback_id, ARCHIE)
    with pytest.raises(ValueError):
        s.service.release(r.feedback_id, s.founder, HEAD)


def test_release_with_a_moved_or_unknown_head_marks_the_feedback_stale(s):
    r = s.approved()
    with pytest.raises(NotEligible):
        s.service.release(r.feedback_id, s.founder, OTHER_HEAD)
    assert s.service.status(r.feedback_id)["state"] == "stale"
    with pytest.raises(ValueError):
        s.service.release(r.feedback_id, s.founder, HEAD)  # stale is terminal: a new review is needed


def test_release_with_an_unknown_head_fails_closed(s):
    r = s.approved()
    with pytest.raises(NotEligible):
        s.service.release(r.feedback_id, s.founder, None)
    assert s.service.status(r.feedback_id)["state"] == "stale"


def test_a_pr_update_after_release_makes_released_feedback_stale_and_unretrievable(s):
    r = s.released()
    assert s.gateway.get(r.feedback_id, "student-one")
    assert s.service.observe_head(SRC, 7, OTHER_HEAD) == [r.feedback_id]
    with pytest.raises(LookupError):
        s.gateway.get(r.feedback_id, "student-one")
    assert s.service.status(r.feedback_id)["stale_reason"] == "head_moved"
    assert s.service.observe_head(SRC, 7, OTHER_HEAD) == []  # idempotent
    assert s.audit.records(event="feedback.stale")


def test_observe_head_ignores_other_prs_and_the_same_commit(s):
    r = s.released()
    assert s.service.observe_head(SRC, 7, HEAD) == [] and s.service.observe_head(SRC, 8, OTHER_HEAD) == []
    assert s.gateway.get(r.feedback_id, "student-one")


def test_new_review_of_the_new_commit_gets_its_own_feedback(tmp_path):
    s = Setup(tmp_path)
    old = s.released()
    s.service.observe_head(SRC, 7, OTHER_HEAD)
    s2 = Setup(tmp_path / "again", comments=[comment(sage_body(head=OTHER_HEAD))], head=OTHER_HEAD)
    s2.service = s.service  # same service and store
    new = s.service.prepare(s2.snapshot())
    assert new.feedback_id != old.feedback_id and new.reviewed_sha == OTHER_HEAD


def test_a_tampered_stored_content_blocks_release(s):
    r = s.approved()
    path = s.root / "private" / r.feedback_id / "content.json"
    data = json.loads(path.read_text())
    data["attempted"] = "Changed after approval"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="changed after approval"):
        s.service.release(r.feedback_id, s.founder, HEAD)
    with pytest.raises(LookupError):
        s.gateway.get(r.feedback_id, "student-one")


def test_a_tampered_released_package_fails_integrity(s):
    r = s.released()
    path = s.root / "released" / f"{r.feedback_id}.json"
    data = json.loads(path.read_text())
    data["segments"][0]["narration"] = "Injected."
    path.write_text(json.dumps(data))
    with pytest.raises(LookupError):
        s.gateway.get(r.feedback_id, "student-one")


def test_revocation_removes_the_package_and_is_founder_only(s):
    r = s.released()
    with pytest.raises(Unauthorized):
        s.service.revoke(r.feedback_id, s.reviewer)
    s.service.revoke(r.feedback_id, s.founder)
    with pytest.raises(LookupError):
        s.gateway.get(r.feedback_id, "student-one")
    assert s.service.revoke(r.feedback_id, s.founder).state is FeedbackState.REVOKED


def test_confidential_text_in_a_review_blocks_release(tmp_path):
    guard = ContentGuard()
    guard.add(KEY_LINE)
    body = sage_body(findings=[finding(severity="low", category="documentation", description=KEY_LINE)])
    s = Setup(tmp_path, comments=[comment(body)], guard=guard)
    r = s.approved()
    with pytest.raises(ValueError, match="confidentiality"):
        s.service.release(r.feedback_id, s.founder, HEAD)
    with pytest.raises(LookupError):
        s.gateway.get(r.feedback_id, "student-one")


def test_prepare_is_idempotent_and_concurrent_calls_make_one_record(s):
    ids = []
    threads = [threading.Thread(target=lambda: ids.append(s.service.prepare(s.snapshot()).feedback_id))
               for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(ids)) == 1 and len(list((s.root / "private").glob("FB-*"))) == 1
    assert len(s.audit.records(event="feedback.prepared")) == 1


def test_repeated_release_is_a_no_op(s):
    r = s.released()
    s.service.release(r.feedback_id, s.founder, HEAD)  # a repeat changes nothing and records nothing
    assert len(s.audit.records(event="feedback.released")) == 1


# -- separation from curriculum ---------------------------------------------------------------------

def test_the_feedback_module_does_not_import_the_curriculum_path():
    tree = ast.parse(Path(fb.__file__).read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"assignment", "lesson", "evidence", "publication", "workflow", "synthesis",
                           "instructor_access", "ada"}, imported


def test_a_full_feedback_run_creates_no_assignment_lesson_or_published_lesson(s, tmp_path):
    from viharbot.learning.assignment import AssignmentStore
    store = AssignmentStore(tmp_path / "assignments", frozenset({"vchokshi"}), require_authenticated=True)
    s.released()
    assert store.list() == []
    assert {p.name for p in tmp_path.iterdir()} <= {"audit", "feedback", "assignments"}  # no drafts/ or published/
    assert not s.audit.records(event="draft.created") and not s.audit.records(event="publication.published")


def test_other_agents_cannot_prepare_feedback(s):
    with pytest.raises(Unauthorized):
        s.service.prepare(s.snapshot(), AgentActor("kepler"))
    with pytest.raises(Unauthorized):
        s.service.submit("FB-000000000000", AgentActor("ada"))


def test_the_feedback_gateway_holds_only_the_released_store(s):
    assert {type(v).__name__ for v in vars(s.gateway).values()} == {"ReleasedFeedbackStore", "AuditLog"}


def test_feedback_status_has_no_student_or_review_text(s):
    r = s.released()
    text = json.dumps(s.service.status(r.feedback_id))
    assert "Sage" not in text and "attempted" not in text
