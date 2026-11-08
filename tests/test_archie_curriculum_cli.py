"""Epic #139: scripts/archie_curriculum.py drives the workflows through the library's own gates. Offline:
fake GitHub transports; the script's `_client` seam is patched."""

from __future__ import annotations

import importlib.util
import json

import pytest

from archie_helpers import FOUNDER_TOKEN, HEAD, INS, OTHER_TOKEN, REVIEWER_TOKEN, SRC, authenticator
from test_archie_feedback import StudentFake
from test_archie_sage_evidence import comment
from test_instructor_access import KEY_LINE, FakeInstructorRepo
from viharbot.learning.assignment import AssignmentStore, EvidenceScope
from viharbot.learning.audit import AuditLog
from viharbot.learning.authn import hash_token
from viharbot.sage_review_github import GitHubClient

FLAGS = ["--quality-reviewed", "--confidentiality-reviewed"]


def load_cli():
    spec = importlib.util.spec_from_file_location("archie_curriculum", "scripts/archie_curriculum.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Rig:
    def __init__(self, monkeypatch, tmp_path, capsys):
        self.cli, self.capsys, self.tmp = load_cli(), capsys, tmp_path
        self.fake = StudentFake(comments=[comment()])
        self.fake.head_sha = HEAD
        self.instructor = FakeInstructorRepo()
        env = monkeypatch.setenv
        env("ASSIGNMENT_STORE_DIR", str(tmp_path / "assign"))
        env("ARCHIE_AUDIT_DIR", str(tmp_path / "audit"))
        env("ARCHIE_DRAFT_DIR", str(tmp_path / "drafts"))
        env("ARCHIE_PUBLISHED_DIR", str(tmp_path / "published"))
        env("ARCHIE_WORKFLOW_DIR", str(tmp_path / "workflows"))
        env("ARCHIE_FEEDBACK_DIR", str(tmp_path / "feedback"))
        env("ARCHIE_FOUNDER_LOGINS", "vchokshi")
        env("ARCHIE_FOUNDER_TOKEN_HASHES", f"vchokshi:{hash_token(FOUNDER_TOKEN)}")
        env("ARCHIE_REVIEWER_TOKEN_HASHES", f"reviewer-one:{hash_token(REVIEWER_TOKEN)},"
                                            f"reviewer-two:{hash_token(OTHER_TOKEN)}")
        env("ARCHIE_STUDENT_LOGINS", "student-one")
        env("ARCHIE_SOURCE_TOKEN", "src-token")
        env("LEARNING_SOURCE_REPO", SRC)
        monkeypatch.delenv("ARCHIE_TOKEN", raising=False)
        monkeypatch.delenv("ARCHIE_INSTRUCTOR_READ_TOKEN", raising=False)
        monkeypatch.delenv("ARCHIE_ALLOW_CONFIDENTIAL", raising=False)
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(self.cli, "_client", self._client)
        self.founder = authenticator().authenticate("vchokshi", FOUNDER_TOKEN)

    def _client(self, token):
        return GitHubClient(token, self.instructor if token == "ins-token" else self.fake)

    def assign(self, **kw):
        store = AssignmentStore(self.tmp / "assign", frozenset({"vchokshi"}), require_authenticated=True)
        return store.assign(self.founder, "t", SRC, (7,), **kw).assignment_id

    def run(self, *argv, token=None):
        if token is None:
            self.monkeypatch.delenv("ARCHIE_TOKEN", raising=False)
        else:
            self.monkeypatch.setenv("ARCHIE_TOKEN", token)
        code = self.cli.main(list(argv))
        out = self.capsys.readouterr()
        return code, out.out, out.err

    def workflow_id(self, out):
        return out.split(":")[0].strip()


@pytest.fixture
def rig(monkeypatch, tmp_path, capsys):
    return Rig(monkeypatch, tmp_path, capsys)


def test_full_curriculum_flow_through_the_cli(rig):
    aid = rig.assign()
    code, out, _ = rig.run("draft", "--assignment", aid, "--pr", "7")
    wf = rig.workflow_id(out)
    assert code == 0 and wf.startswith("WF-") and out.strip().endswith("drafted")
    assert rig.run("submit", wf)[0] == 0
    assert rig.run("approve", "--as", "reviewer-one", *FLAGS, wf, token=REVIEWER_TOKEN)[0] == 0
    assert rig.run("publish", "--as", "vchokshi", wf, token=FOUNDER_TOKEN)[0] == 0
    status = json.loads(rig.run("status", wf)[1])
    assert status["step"] == "published" and status["lesson"]["published"] is True
    code, out, _ = rig.run("ada-get", status["lesson"]["lesson_id"])
    got = json.loads(out)
    assert code == 0 and got["integrity_ok"] and got["delivery_mode"] == "scripted"
    assert got["approved_by"] == "reviewer-one" and got["published_by"] == "vchokshi"
    assert rig.run("audit-verify")[0] == 0
    assert rig.run("revoke", "--as", "vchokshi", wf, token=FOUNDER_TOKEN)[0] == 0
    code, _, err = rig.run("ada-get", status["lesson"]["lesson_id"])
    assert code == 2 and "not available" in err


def test_the_cli_cannot_skip_gates_the_library_enforces(rig):
    aid = rig.assign()
    wf = rig.workflow_id(rig.run("draft", "--assignment", aid, "--pr", "7")[1])
    assert rig.run("publish", "--as", "vchokshi", wf, token=FOUNDER_TOKEN)[0] == 2  # not approved yet
    rig.run("submit", wf)
    assert rig.run("approve", "--as", "reviewer-one", wf, token=REVIEWER_TOKEN)[0] == 2  # no attestation
    assert rig.run("approve", "--as", "reviewer-one", "--quality-reviewed", wf, token=REVIEWER_TOKEN)[0] == 2
    rig.run("approve", "--as", "reviewer-two", *FLAGS, wf, token=OTHER_TOKEN)
    assert rig.run("publish", "--as", "reviewer-one", wf, token=REVIEWER_TOKEN)[0] == 2  # reviewer, not founder
    assert rig.run("revoke", "--as", "reviewer-one", wf, token=REVIEWER_TOKEN)[0] == 2
    code, _, _ = rig.run("ada-get", json.loads(rig.run("status", wf)[1])["lesson"]["lesson_id"])
    assert code == 2  # nothing is published


@pytest.mark.parametrize("token", [None, "wrong-token-" + "x" * 20, REVIEWER_TOKEN])
def test_privileged_commands_need_the_matching_token(rig, token):
    aid = rig.assign()
    wf = rig.workflow_id(rig.run("draft", "--assignment", aid, "--pr", "7")[1])
    rig.run("submit", wf)
    code, _, err = rig.run("approve", "--as", "vchokshi", *FLAGS, wf, token=token)
    assert code == 2 and "refused" in err and FOUNDER_TOKEN not in err


def test_privileged_commands_are_refused_when_no_hashes_are_configured(rig):
    aid = rig.assign()
    wf = rig.workflow_id(rig.run("draft", "--assignment", aid, "--pr", "7")[1])
    rig.run("submit", wf)
    rig.monkeypatch.delenv("ARCHIE_FOUNDER_TOKEN_HASHES")
    rig.monkeypatch.delenv("ARCHIE_REVIEWER_TOKEN_HASHES")
    code, _, err = rig.run("approve", "--as", "vchokshi", *FLAGS, wf, token=FOUNDER_TOKEN)
    assert code == 2 and "no token hashes" in err


def test_draft_needs_an_active_assignment_covering_the_pr_and_a_source_token(rig):
    aid = rig.assign()
    assert rig.run("draft", "--assignment", aid, "--pr", "9")[0] == 2  # not covered
    assert rig.run("draft", "--assignment", "AS-000000000000", "--pr", "7")[0] == 2
    assert rig.fake.calls == []
    rig.monkeypatch.delenv("ARCHIE_SOURCE_TOKEN")
    code, _, err = rig.run("draft", "--assignment", aid, "--pr", "7")
    assert code == 2 and "ARCHIE_SOURCE_TOKEN" in err


def test_draft_with_model_flag_falls_back_to_the_baseline_when_no_alias_is_set(rig):
    rig.monkeypatch.delenv("ARCHIE_MODEL_ALIAS", raising=False)
    aid = rig.assign()
    code, out, _ = rig.run("draft", "--assignment", aid, "--pr", "7", "--model")
    assert code == 0 and out.strip().endswith("drafted")
    assert AuditLog(rig.tmp / "audit").records(event="draft.model_rejected")[0].detail["reason"] == "model_unavailable"


def test_instructor_guidance_assignments_rebuild_the_leak_check_before_the_gates(rig):
    scope = (EvidenceScope.SOURCE_REVISION, EvidenceScope.SAGE_REVIEW, EvidenceScope.GOVERNANCE,
             EvidenceScope.INSTRUCTOR_GUIDANCE)
    aid = rig.assign(permitted_evidence=scope, instructor_paths=("answer-keys/week1.md",))
    rig.monkeypatch.setenv("ARCHIE_INSTRUCTOR_READ_TOKEN", "ins-token")
    rig.monkeypatch.setenv("ARCHIE_ALLOW_CONFIDENTIAL", "1")
    code, out, _ = rig.run("draft", "--assignment", aid, "--pr", "7")
    wf = rig.workflow_id(out)
    assert code == 0
    rig.run("submit", wf)
    rig.monkeypatch.delenv("ARCHIE_INSTRUCTOR_READ_TOKEN")  # a later run without the means to rebuild
    code, _, err = rig.run("approve", "--as", "reviewer-one", *FLAGS, wf, token=REVIEWER_TOKEN)
    assert code == 2 and "leak check" in err
    rig.monkeypatch.setenv("ARCHIE_INSTRUCTOR_READ_TOKEN", "ins-token")
    assert rig.run("approve", "--as", "reviewer-one", *FLAGS, wf, token=REVIEWER_TOKEN)[0] == 0
    assert rig.run("publish", "--as", "vchokshi", wf, token=FOUNDER_TOKEN)[0] == 0
    everything = "".join(p.read_text(errors="ignore") for p in rig.tmp.rglob("*") if p.is_file())
    assert KEY_LINE not in everything and INS  # confidential text never reached any stored file


def test_status_prints_state_and_ids_only(rig):
    aid = rig.assign()
    wf = rig.workflow_id(rig.run("draft", "--assignment", aid, "--pr", "7")[1])
    out = rig.run("status", wf)[1]
    assert '"step": "drafted"' in out and "Agent" not in out and "evaluation_guidance" not in out


def test_feedback_flow_through_the_cli(rig):
    code, out, _ = rig.run("feedback-prepare", "--pr", "7")
    fid = rig.workflow_id(out)
    assert code == 0 and fid.startswith("FB-") and "review_pending" in out
    assert rig.run("feedback-approve", "--as", "reviewer-one", *FLAGS, fid, token=REVIEWER_TOKEN)[0] == 0
    assert rig.run("feedback-release", "--as", "reviewer-one", fid, token=REVIEWER_TOKEN)[0] == 2
    assert rig.run("feedback-release", "--as", "vchokshi", fid, token=FOUNDER_TOKEN)[0] == 0
    code, out, _ = rig.run("ada-feedback", fid, "--student", "student-one")
    assert code == 0 and json.loads(out)["integrity_ok"] and json.loads(out)["reviewed_sha"] == HEAD
    assert rig.run("ada-feedback", fid, "--student", "student-two")[0] == 2
    assert rig.run("feedback-revoke", "--as", "vchokshi", fid, token=FOUNDER_TOKEN)[0] == 0
    assert rig.run("ada-feedback", fid, "--student", "student-one")[0] == 2
    assert json.loads(rig.run("feedback-status", fid)[1])["state"] == "revoked"


def test_feedback_release_checks_the_live_head_and_goes_stale(rig):
    fid = rig.workflow_id(rig.run("feedback-prepare", "--pr", "7")[1])
    rig.run("feedback-approve", "--as", "reviewer-one", *FLAGS, fid, token=REVIEWER_TOKEN)
    rig.fake.head_sha = "e" * 40  # the student pushed after approval
    assert rig.run("feedback-release", "--as", "vchokshi", fid, token=FOUNDER_TOKEN)[0] == 2
    assert json.loads(rig.run("feedback-status", fid)[1])["state"] == "stale"
    assert rig.run("ada-feedback", fid, "--student", "student-one")[0] == 2


def test_feedback_prepare_refuses_a_student_not_on_the_roster(rig):
    rig.monkeypatch.setenv("ARCHIE_STUDENT_LOGINS", "someone-else")
    code, _, err = rig.run("feedback-prepare", "--pr", "7")
    assert code == 2 and "NotEligible" in err
