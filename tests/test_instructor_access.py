"""Issue #141: scoped, read-only instructor-repository access and confidentiality. Offline."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import pytest

from viharbot.learning.assignment import AssignmentStore, NotAssigned
from viharbot.learning.instructor_access import (
    AccessDenied, ConfidentialIndex, ConfidentialLeak, InstructorReader, Sensitivity,
    check_path, classify,
)
from viharbot.sage_review_github import GitHubClient

INS = "vchokshi/viharbot-onboarding-instructor"
SRC = "vchokshi/viharbot"
KEY_LINE = "The correct answer to question four is the off-by-one in the loop bound."


class FakeInstructorRepo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, data):
        path = url.removeprefix("https://api.github.com")
        self.calls.append((method, path))
        assert "Bearer" in headers["Authorization"]
        text = {"knowledge/pr-case-studies/a.md": "A general case study about identities and review.",
                "answer-keys/week1.md": f"# Key\n{KEY_LINE}\n"}
        for name, body in text.items():
            if path == f"/repos/{INS}/contents/{name}":
                return 200, json.dumps({"type": "file", "sha": "b" * 40, "size": len(body),
                                        "content": base64.b64encode(body.encode()).decode()}).encode()
        if path == f"/repos/{INS}/contents/knowledge":
            return 200, json.dumps([
                {"type": "file", "path": "knowledge/readme.md"},
                {"type": "file", "path": "knowledge/answer-key-1.md"},
                {"type": "file", "path": "knowledge/acknowledgments.md"},
                {"type": "dir", "path": "knowledge/pr-case-studies"}]).encode()
        return 404, b""


@pytest.fixture
def setup(tmp_path):
    asg = AssignmentStore(tmp_path / "a", frozenset({"vchokshi"}))
    a = asg.assign("vchokshi", "t", SRC, (7,))
    fake = FakeInstructorRepo()
    return asg, a.assignment_id, fake


def reader(setup, **kw):
    asg, _, fake = setup
    return InstructorReader(GitHubClient("read-tok", fake), INS, asg, **kw)


def test_general_file_is_readable_with_an_active_assignment(setup):
    doc = reader(setup).read(setup[1], "knowledge/pr-case-studies/a.md")
    assert doc.sensitivity is Sensitivity.GENERAL and "case study" in doc.text
    assert all(c[0] == "GET" for c in setup[2].calls)


def test_no_assignment_means_no_read_and_no_call(setup):
    with pytest.raises(NotAssigned):
        reader(setup).read("AS-000000000000", "knowledge/pr-case-studies/a.md")
    with pytest.raises(NotAssigned):
        reader(setup).read("not-an-id", "knowledge/pr-case-studies/a.md")
    assert setup[2].calls == []


def test_cancelled_assignment_cannot_read(setup):
    asg, aid, fake = setup
    asg.cancel("vchokshi", aid)
    with pytest.raises(NotAssigned):
        reader(setup).read(aid, "knowledge/pr-case-studies/a.md")
    assert fake.calls == []


@pytest.mark.parametrize("path", [
    "../etc/passwd", "knowledge/../answer-keys/x.md", "/knowledge/a.md", "knowledge//a.md",
    "knowledge\\a.md", "knowledge/%2e%2e/a.md", "knowledge/.hidden.md", "README.md", "src/app.py",
    "", "knowledge/" + "a" * 250, "knowledge/a.md\x00.png",
])
def test_paths_outside_the_allowlist_or_malformed_are_denied(setup, path):
    with pytest.raises(AccessDenied):
        reader(setup, allow_confidential=True).read(setup[1], path)
    assert setup[2].calls == []


@pytest.mark.parametrize("path", [
    "knowledge/private-acknowledgments.md", "knowledge/acknowledgements/jane.md",
    "knowledge/credentials.md", "lessons/api_token_notes.md", "knowledge/.env", "curriculum/deploy.pem",
])
def test_private_records_and_credentials_are_never_readable_even_when_confidential_is_enabled(setup, path):
    with pytest.raises(AccessDenied):
        reader(setup, allow_confidential=True).read(setup[1], path)
    assert setup[2].calls == []


@pytest.mark.parametrize("path", ["answer-keys/week1.md", "rubrics/r.md", "defects/catalog.md",
                                  "assessments/final.md", "knowledge/grading-notes.md"])
def test_confidential_classes_are_denied_by_default(setup, path):
    assert classify(path) is Sensitivity.CONFIDENTIAL
    with pytest.raises(AccessDenied):
        reader(setup).read(setup[1], path)
    assert setup[2].calls == []


def test_other_repositories_are_refused(setup):
    with pytest.raises(AccessDenied):
        reader(setup).read(setup[1], "knowledge/pr-case-studies/a.md", repo="vchokshi/viharbot-onboarding")
    assert setup[2].calls == []


def test_confidential_read_is_fingerprinted_and_leaks_are_caught(setup):
    r = reader(setup, allow_confidential=True)
    doc = r.read(setup[1], "answer-keys/week1.md")
    assert doc.sensitivity is Sensitivity.CONFIDENTIAL and KEY_LINE in doc.text
    with pytest.raises(ConfidentialLeak):
        r.index.assert_trainee_safe(f"Hint for students:\n  {KEY_LINE.upper()}  \nGood luck.")
    r.index.assert_trainee_safe("Think about how loop bounds are chosen.")  # paraphrase passes (documented)


def test_index_holds_hashes_only_and_ignores_short_lines():
    idx = ConfidentialIndex()
    idx.add("short\n" + KEY_LINE)
    assert KEY_LINE not in repr(vars(idx)) and not idx.leaks("short")


def test_listing_omits_confidential_private_and_directory_entries(setup):
    assert reader(setup).list(setup[1], "knowledge") == ["knowledge/readme.md"]
    assert reader(setup, allow_confidential=True).list(setup[1], "knowledge") == [
        "knowledge/readme.md", "knowledge/answer-key-1.md"]
    with pytest.raises(AccessDenied):
        reader(setup).list(setup[1], "src")


def test_logs_and_repr_never_contain_content_or_paths(setup, caplog):
    r = reader(setup, allow_confidential=True)
    with caplog.at_level(logging.INFO, logger="archie-learning"):
        doc = r.read(setup[1], "answer-keys/week1.md")
        with pytest.raises(AccessDenied):
            r.read(setup[1], "knowledge/acknowledgments.md")
    blob = " ".join(f"{rec.getMessage()} {vars(rec)}" for rec in caplog.records)
    assert "off-by-one" not in blob and "answer-keys" not in blob and "acknowledg" not in blob
    assert "path_hash" in blob and "AccessDenied" in blob
    assert "off-by-one" not in repr(doc) and "answer-keys" not in repr(doc)


def test_reader_has_no_write_call_and_no_other_module_uses_the_read_token():
    text = Path("src/viharbot/learning/instructor_access.py").read_text()
    for forbidden in ('"PUT"', '"POST"', '"PATCH"', '"DELETE"', "/merge", "git/refs"):
        assert forbidden not in text, forbidden
    for p in Path("src").rglob("*.py"):
        if p.name != "instructor_access.py":
            assert "ARCHIE_INSTRUCTOR_READ_TOKEN" not in p.read_text(), p


def test_ada_kepler_and_the_web_app_cannot_reach_instructor_access():
    code = ("import sys, viharbot.web_app, viharbot.execution_bindings as b\n"
            "b.default_bindings()\nassert 'viharbot.learning.instructor_access' not in sys.modules\n")
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_check_path_accepts_only_allowlisted_clean_paths():
    assert check_path("knowledge/pr-case-studies/a.md") == "knowledge/pr-case-studies/a.md"
