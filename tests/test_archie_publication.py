"""Epic #139, issues #144/#151/#145: lifecycle, review, publication, revocation and Ada's boundary.
Offline."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from archie_helpers import FULL_ATTESTATION, make_env
from viharbot.learning import ada as ada_module
from viharbot.learning.ada import AdaGateway, NotAvailable
from viharbot.learning.assignment import NotAssigned
from viharbot.learning.authn import ARCHIE, AgentActor, Unauthorized
from viharbot.learning.lesson import digest_of
from viharbot.learning.publication import (
    Attestation, IllegalTransition, IntegrityError, LessonState, ValidationFailed, validation_codes,
)

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


@pytest.fixture
def lesson(env):
    """A drafted lesson: (assignment, draft)."""
    a = env.assign()
    d = env.draft(a)
    env.service.create_draft(d, ARCHIE)
    return a, d


def submitted(env, lesson):
    _, d = lesson
    env.service.submit(d.lesson_id, 1, ARCHIE)
    return d


def approved(env, lesson):
    d = submitted(env, lesson)
    env.service.approve(d.lesson_id, 1, env.reviewer, FULL_ATTESTATION)
    return d


def published(env, lesson):
    d = approved(env, lesson)
    env.service.publish(d.lesson_id, 1, env.founder)
    return d


# -- lifecycle and authorization ------------------------------------------------------------------

def test_the_happy_path_walks_every_state_in_order(env, lesson):
    d = lesson[1]
    s = env.service
    assert s.status(d.lesson_id, 1)["state"] == "draft"
    assert s.submit(d.lesson_id, 1, ARCHIE).state is LessonState.REVIEW_PENDING
    assert s.approve(d.lesson_id, 1, env.reviewer, FULL_ATTESTATION).state is LessonState.APPROVED
    assert s.publish(d.lesson_id, 1, env.founder).state is LessonState.PUBLISHED
    assert s.revoke(d.lesson_id, 1, env.founder).state is LessonState.REVOKED
    assert [h.op for h in env.drafts.record(d.lesson_id, 1).history] == ["submit", "approve", "publish", "revoke"]


@pytest.mark.parametrize("op", ["approve", "publish", "revoke", "request_changes"])
def test_archie_can_never_approve_publish_revoke_or_request_changes(env, lesson, op):
    d = submitted(env, lesson)
    args = {"approve": (FULL_ATTESTATION,), "publish": (), "revoke": (), "request_changes": ("x",)}[op]
    for agent in (ARCHIE, AgentActor("kepler"), AgentActor("ada")):
        with pytest.raises(Unauthorized):
            getattr(env.service, op)(d.lesson_id, 1, agent, *args)
    assert env.service.status(d.lesson_id, 1)["state"] == "review_pending"
    assert env.audit.records(event="transition.refused")  # each refusal is recorded


def test_other_agents_cannot_even_draft_or_submit(env, lesson):
    _, d = lesson
    for agent in (AgentActor("kepler"), AgentActor("ada"), AgentActor("sage")):
        with pytest.raises(Unauthorized):
            env.service.submit(d.lesson_id, 1, agent)
        with pytest.raises(Unauthorized):
            env.service.create_draft(d, agent)


def test_a_reviewer_may_approve_but_never_publish_or_revoke(env, lesson):
    d = approved(env, lesson)
    for op in ("publish", "revoke"):
        with pytest.raises(Unauthorized):
            getattr(env.service, op)(d.lesson_id, 1, env.reviewer)
    assert env.service.status(d.lesson_id, 1)["state"] == "approved"


def test_the_submitter_cannot_approve_their_own_work(env, lesson):
    _, d = lesson
    env.service.submit(d.lesson_id, 1, env.founder)  # a human submits
    with pytest.raises(Unauthorized):
        env.service.approve(d.lesson_id, 1, env.founder, FULL_ATTESTATION)
    env.service.approve(d.lesson_id, 1, env.reviewer, FULL_ATTESTATION)  # someone else may


def test_approval_needs_both_attestations(env, lesson):
    d = submitted(env, lesson)
    for att in (Attestation(instructional_quality_reviewed=True, confidentiality_reviewed=False),
                Attestation(instructional_quality_reviewed=False, confidentiality_reviewed=True)):
        with pytest.raises(ValidationFailed):
            env.service.approve(d.lesson_id, 1, env.reviewer, att)
    assert env.service.status(d.lesson_id, 1)["state"] == "review_pending"


@pytest.mark.parametrize("path", [
    ["publish"], ["approve"], ["submit", "publish"], ["request_changes"], ["revoke", "publish"]])
def test_publication_cannot_bypass_review(env, lesson, path):
    """Draft → published must fail whatever shortcut is tried."""
    d = lesson[1]
    calls = {"publish": (env.founder,), "approve": (env.reviewer, FULL_ATTESTATION), "submit": (ARCHIE,),
             "request_changes": (env.reviewer,), "revoke": (env.founder,)}
    with pytest.raises(IllegalTransition):
        for op in path:
            getattr(env.service, op)(d.lesson_id, 1, *calls[op])
    assert not env.published.state(d.lesson_id, 1) == "published"
    with pytest.raises(NotAvailable):
        env.gateway.get(d.lesson_id)


def test_changes_requested_sends_the_lesson_back_and_revision_restarts_review(env, lesson):
    d = submitted(env, lesson)
    assert env.service.request_changes(d.lesson_id, 1, env.reviewer, "Needs Work!").state is LessonState.CHANGES_REQUESTED
    with pytest.raises(IllegalTransition):
        env.service.approve(d.lesson_id, 1, env.reviewer, FULL_ATTESTATION)
    revised = d.model_copy(update={"trainee": d.trainee.model_copy(update={"title": "Revised title"})})
    assert env.service.revise(d.lesson_id, 1, revised, ARCHIE).state is LessonState.DRAFT
    env.service.submit(d.lesson_id, 1, ARCHIE)
    assert env.service.approve(d.lesson_id, 1, env.reviewer, FULL_ATTESTATION).state is LessonState.APPROVED
    assert env.audit.records(event="review.changes_requested")[0].detail["reason_code"] == "needswork"


def test_nothing_can_be_revised_while_in_review_or_after_publication(env, lesson):
    d = submitted(env, lesson)
    with pytest.raises(IllegalTransition):
        env.service.revise(d.lesson_id, 1, d, ARCHIE)
    env.service.approve(d.lesson_id, 1, env.reviewer, FULL_ATTESTATION)
    env.service.publish(d.lesson_id, 1, env.founder)
    with pytest.raises(IllegalTransition):
        env.service.revise(d.lesson_id, 1, d, ARCHIE)


def test_revoked_is_terminal(env, lesson):
    d = published(env, lesson)
    env.service.revoke(d.lesson_id, 1, env.founder)
    for call in (lambda: env.service.submit(d.lesson_id, 1, ARCHIE),
                 lambda: env.service.approve(d.lesson_id, 1, env.reviewer, FULL_ATTESTATION),
                 lambda: env.service.publish(d.lesson_id, 1, env.founder),
                 lambda: env.service.revise(d.lesson_id, 1, d, ARCHIE),
                 lambda: env.service.request_changes(d.lesson_id, 1, env.reviewer)):
        with pytest.raises(IllegalTransition):
            call()


# -- stale approval and tampering -----------------------------------------------------------------

def test_editing_after_approval_invalidates_the_approval(env, lesson):
    d = approved(env, lesson)
    edit = d.model_copy(update={"trainee": d.trainee.model_copy(update={"title": "Quietly changed"})})
    rec = env.service.revise(d.lesson_id, 1, edit, ARCHIE)
    assert rec.state is LessonState.DRAFT and rec.approval is None and rec.revision == 2
    with pytest.raises(IllegalTransition):
        env.service.publish(d.lesson_id, 1, env.founder)  # the old approval no longer counts
    assert env.audit.records(event="review.approval_invalidated")


def test_content_changed_on_disk_after_approval_is_detected_at_publish(env, lesson):
    d = approved(env, lesson)
    path = env.drafts.dir(d.lesson_id, 1) / "trainee.json"
    data = json.loads(path.read_text())
    data["title"] = "Tampered after approval"
    path.write_text(json.dumps(data))
    with pytest.raises(IntegrityError):
        env.service.publish(d.lesson_id, 1, env.founder)
    rec = env.drafts.record(d.lesson_id, 1)
    assert rec.state is LessonState.DRAFT and rec.approval is None  # approval invalidated, re-review needed
    with pytest.raises(NotAvailable):
        env.gateway.get(d.lesson_id)
    assert env.audit.records(event="review.approval_invalidated")[-1].detail["reason"] == "content_modified"


def test_instructor_content_changed_after_approval_also_invalidates(env, lesson):
    d = approved(env, lesson)
    path = env.drafts.dir(d.lesson_id, 1) / "instructor.json"
    data = json.loads(path.read_text())
    data["evaluation_guidance"] = ["A different instruction."]
    path.write_text(json.dumps(data))
    with pytest.raises(IntegrityError):
        env.service.publish(d.lesson_id, 1, env.founder)


def test_content_changed_while_awaiting_review_is_caught_at_approval(env, lesson):
    d = submitted(env, lesson)
    path = env.drafts.dir(d.lesson_id, 1) / "trainee.json"
    data = json.loads(path.read_text())
    data["title"] = "Swapped under the reviewer"
    path.write_text(json.dumps(data))
    with pytest.raises(IntegrityError):
        env.service.approve(d.lesson_id, 1, env.reviewer, FULL_ATTESTATION)
    assert env.drafts.record(d.lesson_id, 1).state is LessonState.DRAFT


def test_publication_requires_the_authorising_assignment_to_still_be_active(env, lesson):
    a, _ = lesson
    d = approved(env, lesson)
    env.assignments.cancel(env.founder, a.assignment_id)
    with pytest.raises(NotAssigned):
        env.service.publish(d.lesson_id, 1, env.founder)
    with pytest.raises(NotAvailable):
        env.gateway.get(d.lesson_id)


def test_submission_is_refused_while_the_assignment_is_cancelled(env, lesson):
    a, d = lesson
    env.assignments.cancel(env.founder, a.assignment_id)
    with pytest.raises(NotAssigned):
        env.service.submit(d.lesson_id, 1, ARCHIE)


def test_validation_blocks_review_when_trainee_text_carries_instructor_material(env, lesson):
    _, d = lesson
    leaky = d.trainee.model_copy(update={"limitations": d.trainee.limitations + (
        d.instructor.expected_answers[0].answer,)})
    env.service.revise(d.lesson_id, 1, d.model_copy(update={"trainee": leaky}), ARCHIE)
    with pytest.raises(ValidationFailed) as info:
        env.service.submit(d.lesson_id, 1, ARCHIE)
    assert "instructor_content_in_trainee" in info.value.codes
    assert env.drafts.record(d.lesson_id, 1).state is LessonState.DRAFT


def test_validation_blocks_markers_credentials_and_unsupported_claims(env, lesson):
    _, d = lesson
    from viharbot.learning.basis import Basis
    from viharbot.learning.lesson import Statement
    bad = d.trainee.model_copy(update={
        "limitations": d.trainee.limitations + ("See the answer key.", "token ghp_" + "a" * 30),
        "architecture": d.trainee.architecture + (Statement(text="A guess.", basis=Basis.UNSUPPORTED),)})
    codes = validation_codes(d.model_copy(update={"trainee": bad}))
    assert {"unsupported_statement", "confidentiality:marker", "confidentiality:credential"} <= set(codes)


def test_a_leak_found_after_submission_blocks_approval_and_publication(env, lesson):
    d = submitted(env, lesson)
    env.guard.add(d.trainee.accessibility[0])  # the guard later learns this line is confidential
    with pytest.raises(ValidationFailed) as info:
        env.service.approve(d.lesson_id, 1, env.reviewer, FULL_ATTESTATION)
    assert "confidentiality:exact_line" in info.value.codes
    assert env.drafts.record(d.lesson_id, 1).state is LessonState.REVIEW_PENDING


# -- publication, duplicates, revocation and Ada ---------------------------------------------------

def test_ada_retrieves_only_the_published_trainee_safe_package(env, lesson):
    d = published(env, lesson)
    pkg = env.gateway.get(d.lesson_id)
    assert pkg.content.delivery_mode == "scripted" and pkg.verify()
    assert pkg.provenance.source_sha == d.source_sha and pkg.provenance.approved_by == "reviewer-one"
    assert pkg.provenance.published_by == "vchokshi" and pkg.provenance.approval_revision == 1
    assert pkg.integrity.trainee_digest == digest_of(d.trainee)
    published_files = "\n".join(p.read_text() for p in (env.root / "published").rglob("*.json"))
    for secret in [a.answer for a in d.instructor.expected_answers] + list(d.instructor.evaluation_guidance):
        assert secret not in published_files
    assert "expected_answers" not in published_files and "evaluation_guidance" not in published_files


def test_ada_prompts_are_the_approved_checks_only_and_carry_no_answers(env, lesson):
    d = published(env, lesson)
    pkg = env.gateway.get(d.lesson_id)
    assert [p.prompt_id for p in pkg.content.prompts] == [c.check_id for c in d.trainee.checks]
    assert {s.kind for s in pkg.content.segments} >= {"intro", "concept", "architecture", "checkpoint"}
    assert [n.order for n in pkg.content.navigation] == [s.order for s in pkg.content.segments]
    assert pkg.content.accessibility.captions_required and pkg.content.accessibility.notes


def test_advisory_content_is_labelled_as_opinion_in_the_script(env, lesson):
    d = published(env, lesson)
    scripts = [s.narration for s in env.gateway.get(d.lesson_id).content.segments if s.basis_label == "sage_advisory"]
    assert scripts and all("opinion, not a verified fact" in s for s in scripts)


def test_duplicate_publication_is_idempotent(env, lesson):
    d = published(env, lesson)
    first = env.gateway.get(d.lesson_id)
    again = env.service.publish(d.lesson_id, 1, env.founder)
    assert again.state is LessonState.PUBLISHED
    assert env.gateway.get(d.lesson_id).integrity == first.integrity
    assert len(env.audit.records(event="publication.published")) == 1


def test_concurrent_publications_publish_exactly_once(env, lesson):
    d = approved(env, lesson)
    results, errors = [], []

    def run():
        try:
            results.append(env.service.publish(d.lesson_id, 1, env.founder).state)
        except Exception as exc:  # pragma: no cover - would fail the assertions below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and set(results) == {LessonState.PUBLISHED}
    assert len(env.audit.records(event="publication.published")) == 1 and env.audit.verify()


def test_concurrent_approvals_by_two_reviewers_yield_one_approval(env, lesson):
    d = submitted(env, lesson)
    outcomes = []

    def run(who):
        try:
            outcomes.append(env.service.approve(d.lesson_id, 1, who, FULL_ATTESTATION).approval.approver)
        except IllegalTransition:
            outcomes.append("refused")

    threads = [threading.Thread(target=run, args=(w,)) for w in (env.reviewer, env.reviewer2) * 2]
    [t.start() for t in threads]
    [t.join() for t in threads]
    winner = env.drafts.record(d.lesson_id, 1).approval.approver
    assert outcomes.count(winner) >= 1 and set(outcomes) <= {winner, "refused"}
    assert len(env.audit.records(event="review.approved")) == 1


def test_approve_racing_request_changes_ends_in_one_consistent_state(env, lesson):
    d = submitted(env, lesson)

    def approve():
        try:
            env.service.approve(d.lesson_id, 1, env.reviewer, FULL_ATTESTATION)
        except IllegalTransition:
            pass

    def changes():
        try:
            env.service.request_changes(d.lesson_id, 1, env.reviewer2, "later")
        except IllegalTransition:
            pass

    threads = [threading.Thread(target=f) for f in (approve, changes)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    rec = env.drafts.record(d.lesson_id, 1)
    assert rec.state in (LessonState.APPROVED, LessonState.CHANGES_REQUESTED)
    assert (rec.state is LessonState.APPROVED) == (rec.approval is not None)
    assert env.audit.verify()


def test_revocation_stops_retrieval_immediately_and_removes_the_package(env, lesson):
    d = published(env, lesson)
    assert env.gateway.get(d.lesson_id)
    env.service.revoke(d.lesson_id, 1, env.founder)
    with pytest.raises(NotAvailable):
        env.gateway.get(d.lesson_id)
    with pytest.raises(NotAvailable):
        env.gateway.get(d.lesson_id, version=1)
    assert not (env.root / "published" / d.lesson_id / "v1" / "package.json").exists()
    assert env.published.state(d.lesson_id, 1) == "revoked"
    env.service.revoke(d.lesson_id, 1, env.founder)  # repeating is a no-op
    assert len(env.audit.records(event="publication.revoked")) == 1


def test_an_approved_but_unpublished_lesson_can_be_revoked_and_stays_unavailable(env, lesson):
    d = approved(env, lesson)
    env.service.revoke(d.lesson_id, 1, env.founder)
    with pytest.raises(NotAvailable):
        env.gateway.get(d.lesson_id)


def test_a_revoked_package_that_reappears_on_disk_is_still_refused(env, lesson):
    d = published(env, lesson)
    saved = (env.root / "published" / d.lesson_id / "v1" / "package.json").read_text()
    env.service.revoke(d.lesson_id, 1, env.founder)
    (env.root / "published" / d.lesson_id / "v1" / "package.json").write_text(saved)  # restored by mistake
    with pytest.raises(NotAvailable):
        env.gateway.get(d.lesson_id)  # state.json still says revoked


def test_a_modified_published_package_fails_the_integrity_check(env, lesson):
    d = published(env, lesson)
    path = env.root / "published" / d.lesson_id / "v1" / "package.json"
    data = json.loads(path.read_text())
    data["content"]["segments"][0]["narration"] = "Injected narration."
    path.write_text(json.dumps(data))
    with pytest.raises(NotAvailable):
        env.gateway.get(d.lesson_id)
    assert env.audit.records(event="ada.retrieval_refused")[-1].detail["reason"] == "integrity"


@pytest.mark.parametrize("lesson_id", ["", "../drafts", "a/../b", "UPPER", "x" * 80, None, "nope"])
def test_unknown_or_malformed_lesson_ids_are_refused(env, lesson, lesson_id):
    with pytest.raises(NotAvailable):
        env.gateway.get(lesson_id)


def test_a_lower_version_is_served_until_a_newer_one_is_published(env, lesson):
    d = published(env, lesson)
    assert env.gateway.get(d.lesson_id).content.version == 1
    v2 = env.draft(env.assign()).model_copy(update={"version": 2})
    env.service.create_draft(v2, ARCHIE)
    assert env.gateway.get(d.lesson_id).content.version == 1  # an unpublished v2 is invisible to Ada
    with pytest.raises(NotAvailable):
        env.gateway.get(d.lesson_id, version=2)


def test_status_shows_state_and_ids_but_no_lesson_text(env, lesson):
    d = approved(env, lesson)
    status = env.service.status(d.lesson_id, 1)
    text = json.dumps(status)
    assert status["state"] == "approved" and status["approved_by"] == "reviewer-one"
    assert d.trainee.title not in text and d.instructor.evaluation_guidance[0] not in text


# -- Ada's boundary --------------------------------------------------------------------------------

def test_the_ada_gateway_holds_only_the_published_store(env):
    assert isinstance(env.gateway, AdaGateway)
    held = {type(v).__name__ for v in vars(env.gateway).values()}
    assert held == {"PublishedStore", "AuditLog"}


def test_the_ada_module_imports_nothing_that_reaches_instructor_material():
    tree = ast.parse(Path(ada_module.__file__).read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    forbidden = {"instructor_access", "assignment", "github", "evidence", "publication", "workflow",
                 "synthesis", "authn", "ingest"}
    assert not (imported & forbidden), imported & forbidden
    code = ("import sys, viharbot.learning.ada\n"
            "bad=[m for m in sys.modules if m.startswith('viharbot.learning.') and m.rsplit('.',1)[1] in "
            f"{sorted(forbidden)!r}]\n"
            "assert not bad, bad\n")
    done = subprocess.run([sys.executable, "-c", code], env={"PYTHONPATH": str(SRC_DIR), "PATH": ""},
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr  # even transitively, importing Ada's side loads none of them


def test_nothing_in_the_learning_package_can_merge_or_approve_on_github():
    from viharbot.learning.github import InstructorProposer, SourceReader
    from viharbot.learning.instructor_access import InstructorReader
    from viharbot.learning.publication import PublicationService
    for cls in (InstructorProposer, SourceReader, InstructorReader, PublicationService):
        # verbs only: `merged_pull_requests` is a read of already-merged PRs, not a merge operation
        mutating = [n for n in dir(cls) if re.match(r"_?(merge|approve|close|delete|dismiss)(?!d)", n.lower())
                    and n not in {"approve", "request_changes"}]
        assert not mutating, (cls, mutating)


def test_only_the_workflow_and_cli_import_the_publication_path():
    root = SRC_DIR / "viharbot"
    importers = set()
    for path in root.rglob("*.py"):
        if "learning" in path.parts and path.name in {"workflow.py", "publication.py"}:
            continue
        if "viharbot.learning.publication" in path.read_text() or "from .publication" in path.read_text():
            importers.add(path.name)
    assert importers <= {"workflow.py", "__init__.py"}
    assert not any("learning" in line for line in (root / "web_app.py").read_text().splitlines()
                   if line.startswith(("import", "from")))
    assert not any("learning.workflow" in p.read_text() for p in root.glob("*.py"))
