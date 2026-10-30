"""Contributor registry validation, eligibility and workflow guardrails.

Everything runs offline: GitHub is replaced by a stub and no network is used.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "contributor_registry.py"
WORKFLOW = ROOT / ".github" / "workflows" / "contributor-registry.yml"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
REGISTRY_FILE = ROOT / "CONTRIBUTORS.md"
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason=".github is not in this environment")

spec = importlib.util.spec_from_file_location("contributor_registry", SCRIPT)
cr = importlib.util.module_from_spec(spec)
sys.modules["contributor_registry"] = cr  # dataclasses resolve annotations through it
spec.loader.exec_module(cr)

FOUNDER = (1, "founder")
ALICE = (2002, "alice")
BOB = (3003, "bob")


def row(login: str, account_id: int | str, approved: str, role: str | None = None) -> str:
    """`approved` is the date; "Founder" is shorthand for the founder's role with an N/A date."""
    if approved == "Founder":
        role, approved = "Founder", "N/A"
    return f"| [@{login}](https://github.com/{login}) | {account_id} | {role or 'Trusted contributor'} | {approved} |"


def registry(*rows: str, header: str | None = None) -> str:
    head = header or "| GitHub username | GitHub numeric account ID | Role | Approved on (UTC) |"
    return "\n".join(
        ["# Approved Trusted Contributors", "", "## Registry", "", head, "| --- | ---: | --- | --- |", *rows,
         "", "## How registration works", "", "| not | a | row | x |"]
    ) + "\n"


BASE = registry(row("founder", 1, "Founder"), row("alice", 2002, "2026-10-30"))
WITH_BOB = registry(row("founder", 1, "Founder"), row("alice", 2002, "2026-10-30"), row("bob", 3003, "2026-11-01"))
ONLY_FOUNDER = registry(row("founder", 1, "Founder"))
CHANGED = {"CONTRIBUTORS.md"}


def evaluate(base, head, author=BOB, changed=CHANGED):
    return cr.evaluate(base, head, set(changed), author[0], author[1])


# ---- structure -------------------------------------------------------------------------------
def test_valid_registry_parses():
    entries, errors = cr.parse(BASE)
    assert errors == [] and [(e.username, e.account_id) for e in entries] == [("founder", 1), ("alice", 2002)]
    assert entries[0].is_founder and entries[0].approved == "N/A" and not entries[1].is_founder


def test_the_real_registry_is_valid_when_present():
    if not REGISTRY_FILE.exists():
        pytest.skip("no registry in this checkout")
    entries, errors = cr.parse(REGISTRY_FILE.read_text(encoding="utf-8"))
    assert errors == [] and any(e.is_founder for e in entries)


def test_duplicate_account_ids_and_usernames_are_rejected():
    _, errors = cr.parse(registry(row("founder", 1, "Founder"), row("alice", 1, "2026-10-30")))
    assert any("account ID 1 is already listed" in e for e in errors)
    _, errors = cr.parse(registry(row("founder", 1, "Founder"), row("Founder", 2, "2026-10-30")))
    assert any("already listed" in e and "founder" in e.lower() for e in errors)


@pytest.mark.parametrize("bad", ["0", "012", "abc", "-5", "1.5", "", "12345678901234567", "1 2"])
def test_invalid_account_ids_are_rejected(bad):
    _, errors = cr.parse(registry(row("founder", 1, "Founder"), row("alice", bad, "2026-10-30")))
    assert any("account ID" in e and "positive whole number" in e for e in errors)


@pytest.mark.parametrize("bad", ["-alice", "alice-", "al--ice", "a" * 40, "al ice", "al_ice"])
def test_invalid_usernames_are_rejected(bad):
    _, errors = cr.parse(registry(row("founder", 1, "Founder"), row(bad, 5, "2026-10-30")))
    assert errors


def test_other_structural_problems_are_reported():
    assert cr.parse("# nothing here\n")[1] == ["There is no '## Registry' section."]
    assert cr.parse("## Registry\n\ntext only\n")[1] == ["The Registry section has no table."]
    assert any("header" in e for e in cr.parse(registry(row("founder", 1, "Founder"), header="| a | b | c |"))[1])
    assert any("real date" in e for e in cr.parse(registry(row("a", 1, "Founder"), row("b", 2, "2026-13-45")))[1])
    assert any("Role must be" in e for e in cr.parse(registry(row("a", 1, "Founder"), row("b", 2, "2026-10-30", role="Admin")))[1])
    assert any("must be `N/A`" in e for e in cr.parse(registry("| [@a](https://github.com/a) | 1 | Founder | 2026-10-30 |"))[1])
    assert any("real date" in e for e in cr.parse(registry(row("a", 1, "Founder"), row("b", 2, "N/A")))[1])
    assert any("link target" in e for e in cr.parse(registry("| [@a](https://github.com/b) | 1 | Founder | N/A |"))[1])
    assert any("4 columns" in e for e in cr.parse(registry("| a | 1 | x |"))[1])
    assert any("at least one" in e for e in cr.parse(registry())[1])


# ---- registration ---------------------------------------------------------------------------
def test_new_contributor_registration_is_recognised_but_grants_nothing():
    result = evaluate(BASE, WITH_BOB)
    assert result.ok and result.kind == "registration"
    assert result.author_state == "registration-request"
    assert any("does NOT make the author a trusted contributor" in n for n in result.notices)
    # Eligibility still comes only from the base registry, where bob is absent.
    assert cr.eligibility(BASE, BOB[0]) == "external"


def test_self_authorization_attempts_are_rejected():
    # Adding a row for someone else, or under someone else's account ID.
    carol = registry(row("founder", 1, "Founder"), row("alice", 2002, "2026-10-30"), row("carol", 4004, "2026-11-01"))
    assert any("may register only yourself" in e for e in evaluate(BASE, carol).errors)
    spoof = registry(row("founder", 1, "Founder"), row("alice", 2002, "2026-10-30"), row("bob", 2002, "2026-11-01"))
    assert any("account ID 2002 is already listed" in e for e in evaluate(BASE, spoof).errors)
    # Claiming the founder marker, adding two rows, or sneaking in other files.
    founder_claim = registry(row("founder", 1, "Founder"), row("alice", 2002, "2026-10-30"),
                             row("bob", 3003, "N/A", role="Founder"))
    assert any("role cannot be claimed" in e for e in evaluate(BASE, founder_claim).errors)
    two = registry(row("founder", 1, "Founder"), row("alice", 2002, "2026-10-30"), row("bob", 3003, "2026-11-01"),
                   row("carol", 4004, "2026-11-01"))
    assert any("only one row" in e for e in evaluate(BASE, two).errors)
    assert any("other files" in e for e in evaluate(BASE, WITH_BOB, changed={"CONTRIBUTORS.md", "src/x.py"}).errors)


def test_bootstrap_submission_by_an_unlisted_author_is_allowed():
    # The author is not in the registry; the check must not create an impossible loop.
    assert evaluate(BASE, WITH_BOB, author=BOB).ok


def test_unauthorized_modification_of_existing_records_is_rejected():
    renamed = registry(row("founder", 1, "Founder"), row("mallory", 2002, "2026-10-30"))
    redated = registry(row("founder", 1, "Founder"), row("alice", 2002, "2020-01-01"))
    promoted = registry(row("bob", 1, "Founder"), row("alice", 2002, "2026-10-30"))
    for head in (renamed, redated, promoted):
        result = evaluate(BASE, head)
        assert not result.ok and any("modified" in e for e in result.errors)
    # An ordinary trusted contributor is not the founder and may not edit others either.
    assert not evaluate(BASE, renamed, author=ALICE).ok
    swap = registry(row("founder", 1, "Founder"), row("bob", 3003, "2026-11-01"))
    assert not evaluate(BASE, swap, author=BOB).ok  # removes alice and adds bob in one go


def test_the_founder_may_maintain_the_registry():
    result = evaluate(BASE, WITH_BOB, author=FOUNDER)
    assert result.ok and result.kind == "founder-change"
    assert evaluate(BASE, registry(row("founder", 1, "Founder"), row("alice", 2002, "2026-12-01")), author=FOUNDER).ok


def test_a_pr_cannot_pass_by_claiming_to_be_the_founder_in_its_own_registry():
    # The founder row is read from the base only; a head that crowns bob changes nothing.
    head = registry(row("founder", 1, "2026-01-01"), row("alice", 2002, "2026-10-30"),
                    row("bob", 3003, "N/A", role="Founder"))
    assert not evaluate(BASE, head, author=BOB).ok


# ---- removal and eligibility ------------------------------------------------------------------
def test_contributor_removal_is_allowed_and_ends_eligibility():
    result = evaluate(BASE, ONLY_FOUNDER, author=ALICE)
    assert result.ok and result.kind == "removal"
    assert any("does not revoke GitHub" in n for n in result.notices)
    assert cr.eligibility(BASE, ALICE[0]) == "trusted"            # before the removal merges
    assert cr.eligibility(ONLY_FOUNDER, ALICE[0]) == "external"   # after it merges
    mixed = registry(row("alice", 2002, "2026-10-30"), row("bob", 3003, "2026-11-01"))
    assert not evaluate(BASE, mixed, author=BOB).ok


def test_ordinary_external_prs_are_unaffected():
    result = evaluate(BASE, BASE, author=BOB, changed={"src/viharbot/web_app.py"})
    assert result.ok and result.kind == "none" and result.author_state == "external"
    assert evaluate(BASE, BASE, author=ALICE, changed={"README.md"}).author_state == "trusted"


def test_registry_deletion_and_a_broken_head_or_base_fail():
    assert not evaluate(BASE, None).ok
    assert not evaluate(BASE, "garbage").ok
    assert any("base" in e for e in evaluate("garbage", WITH_BOB).errors)


def test_eligibility_never_trusts_a_pr_supplied_registry(tmp_path):
    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                       cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    (tmp_path / "CONTRIBUTORS.md").write_text(BASE)
    git("add", "."); git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "pr")
    (tmp_path / "CONTRIBUTORS.md").write_text(WITH_BOB)   # the PR crowns bob in the working tree
    git("commit", "-qam", "bob adds himself")
    cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        assert cr.main(["eligible", "--git-ref", "main", "--account-id", "3003"]) == 1   # external
        assert cr.main(["eligible", "--git-ref", "main", "--account-id", "2002"]) == 0   # trusted
        assert cr.main(["eligible", "--git-ref", "pr", "--account-id", "3003"]) == 0     # only if asked
        assert cr.main(["eligible", "--git-ref", "no-such-ref", "--account-id", "2002"]) == 1
    finally:
        os.chdir(cwd)


def test_an_invalid_trusted_registry_refuses_to_decide(capsys):
    with pytest.raises(ValueError):
        cr.eligibility("garbage", 1)
    assert cr.eligibility(None, 1) == "external"


# ---- GitHub identity verification (mocked) ---------------------------------------------------
def entry(login="bob", account_id=3003):
    return cr.Entry(login, account_id, "Trusted contributor", "2026-11-01")


def test_identity_verification_accepts_a_matching_account():
    assert cr.verify_identities([entry()], lambda i: "Bob") == []


def test_identity_verification_reports_a_mismatch_or_missing_account():
    assert "currently `someoneelse`" in cr.verify_identities([entry()], lambda i: "someoneelse")[0]
    assert "no account with ID 3003" in cr.verify_identities([entry()], lambda i: None)[0]


def test_unavailable_github_fails_closed_instead_of_passing():
    def down(_):
        raise cr.IdentityUnavailable("HTTP 503")

    problems = cr.verify_identities([entry()], down)
    assert problems and "Could not verify" in problems[0] and "rather than assuming" in problems[0]


def test_check_fails_when_verification_is_unavailable_and_passes_when_it_works(tmp_path, capsys):
    (tmp_path / "base.md").write_text(BASE)
    (tmp_path / "head.md").write_text(WITH_BOB)
    args = argparse.Namespace(base_file=str(tmp_path / "base.md"), head_file=str(tmp_path / "head.md"),
                              author_id=3003, author_login="bob", changed_files="CONTRIBUTORS.md", no_verify=False)

    def down(_):
        raise cr.IdentityUnavailable("timeout")

    assert cr.run_check(args, fetch_user=down) == 1
    assert "FAIL" in capsys.readouterr().out
    assert cr.run_check(args, fetch_user=lambda i: "bob") == 0
    assert "PASS" in capsys.readouterr().out
    assert cr.run_check(args, fetch_user=lambda i: "impostor") == 1


def test_a_check_with_no_registry_change_does_not_call_github(tmp_path):
    (tmp_path / "base.md").write_text(BASE)
    args = argparse.Namespace(base_file=str(tmp_path / "base.md"), head_file=str(tmp_path / "base.md"),
                              author_id=3003, author_login="bob", changed_files="README.md", no_verify=False)

    def boom(_):
        raise AssertionError("GitHub must not be called")

    assert cr.run_check(args, fetch_user=boom) == 0


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, *a):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_github_lookup_maps_http_outcomes(monkeypatch):
    def fake(outcome):
        def opener(request, timeout=None):
            assert request.full_url == "https://api.github.com/user/3003"
            if isinstance(outcome, Exception):
                raise outcome
            return FakeResponse(outcome)
        return opener

    monkeypatch.setattr(cr.urllib.request, "urlopen", fake(b'{"login": "bob"}'))
    assert cr.fetch_github_login(3003, token="t") == "bob"
    monkeypatch.setattr(cr.urllib.request, "urlopen", fake(urllib.error.HTTPError("u", 404, "nf", {}, None)))
    assert cr.fetch_github_login(3003) is None
    for failure in (urllib.error.HTTPError("u", 403, "rate", {}, None), urllib.error.HTTPError("u", 500, "x", {}, None),
                    urllib.error.URLError("down"), TimeoutError(), ValueError()):
        monkeypatch.setattr(cr.urllib.request, "urlopen", fake(failure))
        with pytest.raises(cr.IdentityUnavailable):
            cr.fetch_github_login(3003)


def test_untrusted_text_is_neutralised_in_reports():
    hostile = "bad\n::error::pwned`x"
    assert "\n" not in cr.safe(hostile) and "`" not in cr.safe(hostile)
    head = registry(row("founder", 1, "Founder"), row("alice", 2002, "2026-10-30"), row("bob", 3003, "2026-11-01"))
    result = evaluate(BASE, head, author=(3003, hostile))
    assert "\n::error::" not in "\n".join(result.errors)


# ---- workflow and ownership guardrails -------------------------------------------------------
def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def run_blocks(text: str) -> list[str]:
    blocks, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        m = re.match(r"^(\s*)run:\s*\|\s*$", lines[i])
        if m:
            indent, body, i = len(m.group(1)), [], i + 1
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
                body.append(lines[i]); i += 1
            blocks.append("\n".join(body))
        else:
            i += 1
    return blocks


def test_workflow_uses_pull_request_target_without_running_pr_code():
    text = workflow_text()
    assert "pull_request_target:" in text and "branches: [main]" in text
    assert "paths:" not in text  # must report on every PR so it can be a required check
    # The only checkout is of the base commit, and never of the pull request head.
    assert text.count("actions/checkout@") == 1
    assert "ref: ${{ github.event.pull_request.base.sha }}" in text
    assert "persist-credentials: false" in text
    assert "pull_request.head" not in text
    # PR content is only fetched as data, never executed or installed.
    for forbidden in ("pip install", "npm ", "make ", "bash FETCH_HEAD", "source ", "docker "):
        assert forbidden not in text
    assert "python3 .github/scripts/contributor_registry.py" in text


def test_workflow_has_least_privilege_and_no_secrets():
    text = workflow_text()
    assert "secrets." not in text
    assert re.search(r"^permissions:\n  contents: read\n", text, re.M)
    perms = re.findall(r"^\s+(\w[\w-]*): (read|write|none)\s*$", text, re.M)
    assert all(level == "read" for _, level in perms)
    assert "write" not in re.findall(r"(?m)^\s*[\w-]+: (write)\b", text)


def test_workflow_passes_event_data_through_env_not_inline_expressions():
    for block in run_blocks(workflow_text()):
        assert "${{" not in block


def test_workflow_actions_are_pinned_to_commit_shas():
    uses = re.findall(r"uses:\s*(\S+)", workflow_text())
    assert uses and all(re.fullmatch(r".+@[0-9a-f]{40}", u) for u in uses)


def test_codeowners_covers_the_registry_and_its_enforcement_files():
    lines = [ln.split() for ln in CODEOWNERS.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    owned = {pattern: owners for pattern, *owners in lines}
    assert owned["/CONTRIBUTORS.md"] == ["@vchokshi"]
    assert owned["/.github/"] == ["@vchokshi"]


def test_script_has_no_dependencies_outside_the_standard_library():
    imports = set(re.findall(r"(?m)^(?:from|import) (\w+)", SCRIPT.read_text()))
    assert imports <= {"argparse", "datetime", "json", "os", "re", "subprocess", "sys", "urllib", "dataclasses",
                       "collections", "__future__"}
