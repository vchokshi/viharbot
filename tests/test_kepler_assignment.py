"""Kepler assignment workflow (issue #128): authorization, workspaces, branch and push limits,
protected paths, draft PRs, audit integrity and persistence. Offline: local bare repo as remote,
fake GitHub, no model calls."""

from __future__ import annotations

import json
import logging
import shutil
import sys
import threading
from pathlib import Path

import pytest
from kepler_helpers import ROOT, TOKEN, git, make_world, ready_run, workspace_of

from viharbot.kepler.audit import AuditIntegrityError, build_event, verify_chain
from viharbot.kepler.policy import AssignmentDenied, KeplerPolicy
from viharbot.kepler.service import AssignmentService
from viharbot.kepler.store import RunStore, StoreError
from viharbot.kepler.workspace import Runner
from viharbot.kepler_governance import GovernanceCitation

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(not (ROOT / "docs" / "governance").exists(), reason="docs/ is not here"),
]
CITE = (GovernanceCitation("constitution", "Section 2.4 — Human Approval", "Founder merges."),)


def reason(call, *args, **kwargs) -> tuple[str, int]:
    with pytest.raises(AssignmentDenied) as info:
        call(*args, **kwargs)
    return info.value.reason, info.value.status


def write(world, run, rel: str, text: str) -> Path:
    path = workspace_of(world, run) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def remote_branches(world) -> set[str]:
    out = git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=world.remote)
    return set(out.split())


# ---- policy -------------------------------------------------------------------------------


def test_instructor_repository_can_never_be_configured():
    with pytest.raises(ValueError):
        KeplerPolicy(repository="vchokshi/viharbot-onboarding-instructor")
    with pytest.raises(ValueError):
        KeplerPolicy(repository="VChokshi/ViharBot-Onboarding-Instructor")


@pytest.mark.parametrize("bad", ["", "no-slash", "a/b/c", "a b/c", "../x", "owner/"])
def test_repository_must_be_owner_slash_name(bad):
    with pytest.raises(ValueError):
        KeplerPolicy(repository=bad)


def test_branch_is_derived_from_the_issue_number_only():
    policy = KeplerPolicy()
    assert policy.branch_for(128) == "kepler/issue-128"
    policy.check_branch("kepler/issue-128", 128)
    for branch, issue in [("main", 128), ("master", 1), ("kepler/issue-129", 128),
                          ("kepler/issue-128/extra", 128), ("feature/x", 128), ("HEAD", 128),
                          ("kepler/issue-128 ", 128), ("", 128)]:
        with pytest.raises(AssignmentDenied):
            policy.check_branch(branch, issue)


# ---- authorization ------------------------------------------------------------------------


def test_only_a_policy_assigner_can_start_a_run(tmp_path):
    world = make_world(tmp_path)
    assert reason(world.service.authorize, 128, actor="intruder") == ("not_authorized", 403)
    assert reason(world.service.authorize, 128, actor="") == ("not_authorized", 403)
    assert world.store.runs() == []
    assert not any(c[0] == "get_issue" for c in world.github.calls)  # denied before any lookup


@pytest.mark.parametrize("issue", [0, -1, True, "128", None, 1.5])
def test_invalid_issue_numbers_are_rejected(tmp_path, issue):
    world = make_world(tmp_path)
    assert reason(world.service.authorize, issue, actor="founder")[0] == "invalid_issue"


@pytest.mark.parametrize("issue", [50, 51, 99999])
def test_only_open_real_issues_are_assignable(tmp_path, issue):
    world = make_world(tmp_path)  # 50 is a pull request, 51 closed, 99999 unknown
    assert reason(world.service.authorize, issue, actor="founder") == ("issue_not_assignable", 422)
    assert world.store.runs() == []


def test_github_failure_denies_without_creating_a_run(tmp_path):
    world = make_world(tmp_path)
    world.github.fail = True
    assert reason(world.service.authorize, 128, actor="founder") == ("github_unavailable", 502)
    assert world.store.runs() == []


def test_authorize_creates_one_run_with_a_derived_branch(tmp_path):
    world = make_world(tmp_path)
    run = world.service.authorize(128, actor="founder", request_id="req-1")
    assert run["state"] == "authorized" and run["branch"] == "kepler/issue-128"
    assert run["created_by"] == "founder" and run["issue"] == 128
    assert "workspace" not in run  # internal paths are not exposed


def test_a_second_active_run_for_the_same_issue_is_refused_until_cancelled(tmp_path):
    world = make_world(tmp_path)
    first = world.service.authorize(128, actor="founder")
    assert reason(world.service.authorize, 128, actor="founder") == ("active_run_exists", 409)
    world.service.authorize(129, actor="founder")  # a different issue is independent
    world.service.cancel(first["run_id"], actor="founder")
    assert world.service.authorize(128, actor="founder")["run_id"] != first["run_id"]


def test_concurrent_authorizations_create_exactly_one_run(tmp_path):
    world = make_world(tmp_path)
    results: list[object] = []

    def go():
        try:
            results.append(world.service.authorize(128, actor="founder"))
        except AssignmentDenied as denied:
            results.append(denied.reason)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count("active_run_exists") == 7
    assert len(world.store.runs()) == 1


def test_there_is_no_unsolicited_work(tmp_path):
    world = make_world(tmp_path)
    ready_run(world)
    # nothing in the service starts a run on its own; only authorize() creates one
    assert len(world.store.runs()) == 1
    assert [n for n in dir(AssignmentService) if "start" in n or "next" in n or "auto" in n] == []


# ---- workspace ----------------------------------------------------------------------------


def test_workspace_is_a_fresh_clone_on_the_feature_branch_per_issue(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    other = ready_run(world, 129)
    first, second = workspace_of(world, run), workspace_of(world, other)
    assert first != second and first.parent == second.parent == world.root.resolve()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=first) == "kepler/issue-128"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=second) == "kepler/issue-129"
    assert (first / "src" / "app.py").exists()
    record = world.store.get_run(run["run_id"])
    assert record["state"] == "workspace_ready" and set(record["governance"]) == {
        "charter", "constitution", "architecture", "skills-registry", "change-rationale"}


def test_missing_governance_fails_the_run_closed(tmp_path):
    world = make_world(tmp_path, with_governance=False)
    run = world.service.authorize(128, actor="founder")
    assert reason(world.service.prepare_workspace, run["run_id"])[0] == "workspace_failed"
    assert world.service.get(run["run_id"])["state"] == "failed"
    # the failed run no longer blocks the issue, but nothing can be committed or pushed on it
    assert reason(world.service.commit, run["run_id"], "x")[0] == "invalid_state"


def test_clone_failure_fails_the_run(tmp_path):
    world = make_world(tmp_path)
    shutil.rmtree(world.remote)
    run = world.service.authorize(128, actor="founder")
    assert reason(world.service.prepare_workspace, run["run_id"])[0] == "workspace_failed"
    assert world.service.get(run["run_id"])["state"] == "failed"


def test_prepare_twice_is_refused(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    assert reason(world.service.prepare_workspace, run["run_id"]) == ("invalid_state", 409)


def test_workspace_cannot_escape_the_root(tmp_path):
    from viharbot.kepler.workspace import Workspace

    world = make_world(tmp_path)
    for name in ("../evil", "a/b", "/abs", ".."):
        with pytest.raises(AssignmentDenied):
            Workspace(world.root, name, world.policy, Runner())


# ---- commits and protected paths ----------------------------------------------------------


def test_commit_records_the_change_and_audit(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    write(world, run, "src/app.py", "print('changed')\n")
    sha = world.service.commit(run["run_id"], "Change the greeting")
    assert len(sha) == 40 and world.store.get_run(run["run_id"])["commits"] == [sha]
    assert git("log", "-1", "--format=%an", cwd=workspace_of(world, run)) == "Kepler"


def test_commit_with_no_changes_is_refused(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    assert reason(world.service.commit, run["run_id"], "x") == ("nothing_to_commit", 409)


@pytest.mark.parametrize("rel", ["docs/governance/agent-constitution-v1.md", "AGENTS.md",
                                 "docs/adr/ADR-new.md", "docs/governance/brand-new.md"])
def test_governance_changes_are_refused_and_unstaged(tmp_path, rel):
    world = make_world(tmp_path)
    run = ready_run(world)
    write(world, run, "src/ok.py", "x = 1\n")  # an allowed change alongside the forbidden one
    write(world, run, rel, "tampered\n")
    assert reason(world.service.commit, run["run_id"], "sneaky") == ("protected_path", 403)
    ws = workspace_of(world, run)
    assert git("diff", "--cached", "--name-only", cwd=ws) == ""  # nothing left staged
    assert git("rev-list", "--count", "origin/main..HEAD", cwd=ws) == "0"  # nothing committed


def test_symlink_alias_of_governance_is_refused(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    ws = workspace_of(world, run)
    (ws / "innocent.md").write_text("x\n")
    (ws / "innocent.md").unlink()
    (ws / "innocent.md").symlink_to(ws / "docs/governance/agent-constitution-v1.md")
    assert reason(world.service.commit, run["run_id"], "alias")[0] == "protected_path"


def test_symlink_escaping_the_workspace_is_refused(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    (workspace_of(world, run) / "link").symlink_to(tmp_path)
    assert reason(world.service.commit, run["run_id"], "escape")[0] == "protected_path"


def test_repository_hooks_do_not_run(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    ws = workspace_of(world, run)
    hook = ws / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path}/hook-ran\nexit 1\n")
    hook.chmod(0o755)
    write(world, run, "src/app.py", "print('x')\n")
    world.service.commit(run["run_id"], "hooks are off")
    assert not (tmp_path / "hook-ran").exists()


# ---- push: feature branch only ------------------------------------------------------------


def test_push_updates_only_the_feature_branch_never_main(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    main_before = git("rev-parse", "main", cwd=world.remote)
    write(world, run, "src/app.py", "print('feature')\n")
    world.service.commit(run["run_id"], "Feature")
    pushed = world.service.push(run["run_id"])
    assert pushed["state"] == "branch_pushed"
    assert remote_branches(world) == {"main", "kepler/issue-128"}
    assert git("rev-parse", "main", cwd=world.remote) == main_before


def test_push_with_no_commits_is_refused(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    assert reason(world.service.push, run["run_id"]) == ("no_commits", 409)
    assert remote_branches(world) == {"main"}


def test_a_tampered_branch_in_the_store_cannot_reach_main(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    write(world, run, "src/app.py", "print('evil')\n")
    world.service.commit(run["run_id"], "Evil")
    main_before = git("rev-parse", "main", cwd=world.remote)
    record = world.store.get_run(run["run_id"])
    record["branch"] = "main"
    world.store.commit(run=record)
    assert reason(world.service.push, run["run_id"]) == ("branch_not_allowed", 403)
    assert git("rev-parse", "main", cwd=world.remote) == main_before
    assert remote_branches(world) == {"main"}


def test_workspace_push_refuses_any_non_feature_branch_before_running_git(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    from viharbot.kepler.workspace import Workspace

    ws = Workspace(world.root, workspace_of(world, run).name, world.policy, Runner())
    for branch in ("main", "master", "kepler/issue-999", "kepler/issue-128;rm"):
        with pytest.raises(AssignmentDenied):
            ws.push(branch, 128)
    assert remote_branches(world) == {"main"}


def test_push_failure_fails_the_run_with_a_scrubbed_message(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    write(world, run, "src/app.py", "print('x')\n")
    world.service.commit(run["run_id"], "x")
    shutil.rmtree(world.remote)
    assert reason(world.service.push, run["run_id"]) == ("push_failed", 502)
    assert world.service.get(run["run_id"])["state"] == "failed"
    assert TOKEN not in json.dumps(world.store.data)


# ---- draft PR -----------------------------------------------------------------------------


def pushed_run(world, issue=128):
    run = ready_run(world, issue)
    write(world, run, "src/app.py", f"print('{issue}')\n")
    world.service.commit(run["run_id"], "Work")
    world.service.push(run["run_id"])
    return run


def test_draft_pr_has_issue_reference_and_governance_section(tmp_path):
    world = make_world(tmp_path)
    run = pushed_run(world)
    view = world.service.open_draft_pr(run["run_id"], title="Implement #128", summary="Did it.",
                                       citations=CITE)
    pull = world.github.pulls[0]
    assert pull["draft"] is True and pull["head"] == "kepler/issue-128"
    assert pull["body"].startswith("Refs #128") and "## Governance followed" in pull["body"]
    assert "Section 2.4" in pull["body"]
    assert view["state"] == "draft_pr_open" and view["pr_number"] == pull["number"]
    assert view["pr_url"] == pull["url"]


def test_second_call_updates_the_same_pr_instead_of_creating_another(tmp_path):
    world = make_world(tmp_path)
    run = pushed_run(world)
    world.service.open_draft_pr(run["run_id"], title="One", summary="a", citations=CITE)
    write(world, run, "src/app.py", "print('more')\n")
    world.service.commit(run["run_id"], "More")
    world.service.push(run["run_id"])
    assert world.service.get(run["run_id"])["state"] == "draft_pr_open"
    world.service.open_draft_pr(run["run_id"], title="Two", summary="b", citations=CITE)
    assert len(world.github.pulls) == 1 and world.github.pulls[0]["title"] == "Two"


def test_an_existing_pr_for_the_branch_is_adopted_after_a_crash(tmp_path):
    world = make_world(tmp_path)
    run = pushed_run(world)
    world.github.create_draft_pull_request("kepler/issue-128", "orphan", "b")  # created, not saved
    world.service.open_draft_pr(run["run_id"], title="T", summary="s", citations=CITE)
    assert len(world.github.pulls) == 1 and world.github.pulls[0]["title"] == "T"


def test_pr_requires_a_pushed_branch(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    assert reason(world.service.open_draft_pr, run["run_id"], title="T", summary="s",
                  citations=CITE) == ("branch_not_pushed", 409)
    assert world.github.pulls == []


@pytest.mark.parametrize("citations", [
    (), (GovernanceCitation("constitution", "Section 99 — Invented", "x"),),
    (GovernanceCitation("constitution", "Section 2.4 — Human Approval", " "),)])
def test_pr_requires_valid_governance_citations(tmp_path, citations):
    world = make_world(tmp_path)
    run = pushed_run(world)
    assert reason(world.service.open_draft_pr, run["run_id"], title="T", summary="s",
                  citations=citations) == ("invalid_governance_citations", 422)
    assert world.github.pulls == []


@pytest.mark.parametrize("title,summary", [("", "s"), ("t", " "), ("x" * 201, "s"),
                                           ("t", "x" * 20_001), (None, "s")])
def test_pr_text_is_validated(tmp_path, title, summary):
    world = make_world(tmp_path)
    run = pushed_run(world)
    assert reason(world.service.open_draft_pr, run["run_id"], title=title, summary=summary,
                  citations=CITE)[0] in {"invalid_title", "invalid_summary"}


def test_pr_is_refused_if_governance_changed_in_the_workspace(tmp_path):
    world = make_world(tmp_path)
    run = pushed_run(world)
    path = workspace_of(world, run) / "docs/governance/agent-constitution-v1.md"
    path.write_text(path.read_text() + "\nedited outside the service\n")
    assert reason(world.service.open_draft_pr, run["run_id"], title="T", summary="s",
                  citations=CITE) == ("governance_changed", 409)
    assert world.github.pulls == []


def test_the_github_surface_cannot_merge_close_or_force(tmp_path):
    from kepler_helpers import FakeGitHub

    from viharbot.kepler.github import GitHubClient, HttpGitHubClient

    banned = ("merge", "close", "delete", "approve", "force", "reopen", "dismiss")
    for cls in (GitHubClient, HttpGitHubClient, FakeGitHub):
        public = [n for n in dir(cls) if not n.startswith("_")]
        assert not [n for n in public if any(b in n.lower() for b in banned)], cls
    for name in dir(AssignmentService):
        assert not any(b in name.lower() for b in banned), name


# ---- checks (tests/lint) ------------------------------------------------------------------


def test_only_allowlisted_commands_run_and_results_are_recorded(tmp_path):
    world = make_world(tmp_path, test_commands=((sys.executable, "-c"),))
    run = ready_run(world)
    for argv in (["rm", "-rf", "/"], ["git", "push"], [], ["sh", "-c", "id"],
                 [sys.executable, "-m", "http.server"]):
        assert reason(world.service.run_checks, run["run_id"], argv) == ("command_not_allowed", 403)
    ok = world.service.run_checks(run["run_id"], [sys.executable, "-c", "print('green')"])
    bad = world.service.run_checks(run["run_id"], [sys.executable, "-c", "raise SystemExit(3)"])
    assert ok["exit_code"] == 0 and "green" in ok["output_tail"]
    assert bad["exit_code"] == 3  # a real failure is reported, never papered over
    events = [e for e in world.store.events() if e["event"] == "tests_completed"]
    assert [e["exit_code"] for e in events] == [0, 3]
    assert [e["decision"] for e in events] == ["allow", "fail"]


def test_checks_do_not_receive_the_github_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("LITELLM_API_KEY", "sk-should-not-leak")
    world = make_world(tmp_path, test_commands=((sys.executable, "-c"),))
    run = ready_run(world)
    code = "import os;print(sorted(os.environ))"
    out = world.service.run_checks(run["run_id"], [sys.executable, "-c", code])["output_tail"]
    assert "GITHUB_TOKEN" not in out and "LITELLM" not in out and TOKEN not in out


def test_check_output_is_scrubbed_of_the_token(tmp_path):
    world = make_world(tmp_path, test_commands=((sys.executable, "-c"),))
    run = ready_run(world)
    out = world.service.run_checks(run["run_id"], [sys.executable, "-c", f"print('{TOKEN}')"])
    assert TOKEN not in out["output_tail"] and "***" in out["output_tail"]


def test_a_timeout_is_recorded_as_a_failure(tmp_path):
    world = make_world(tmp_path, test_commands=((sys.executable, "-c"),), command_timeout_seconds=1)
    world.service.runner = Runner(token=TOKEN, timeout=1)
    run = ready_run(world)
    out = world.service.run_checks(
        run["run_id"], [sys.executable, "-c", "import time;time.sleep(5)"])
    assert out["exit_code"] == 124


# ---- cancel and state machine -------------------------------------------------------------


def test_only_an_assigner_can_cancel_and_cancellation_is_final(tmp_path):
    world = make_world(tmp_path)
    run = ready_run(world)
    assert reason(world.service.cancel, run["run_id"], actor="intruder") == ("not_authorized", 403)
    assert world.service.cancel(run["run_id"], actor="founder")["state"] == "cancelled"
    for call in (lambda: world.service.commit(run["run_id"], "x"),
                 lambda: world.service.push(run["run_id"]),
                 lambda: world.service.run_checks(run["run_id"], ["ruff", "check"]),
                 lambda: world.service.prepare_workspace(run["run_id"]),
                 lambda: world.service.cancel(run["run_id"], actor="founder")):
        assert reason(call)[0] == "invalid_state"


def test_unknown_run_is_a_404(tmp_path):
    world = make_world(tmp_path)
    assert reason(world.service.get, "nope") == ("unknown_run", 404)
    assert reason(world.service.cancel, "nope", actor="founder") == ("unknown_run", 404)


def test_concurrent_prepare_runs_only_once(tmp_path):
    world = make_world(tmp_path)
    run = world.service.authorize(128, actor="founder")
    outcomes: list[str] = []

    def go():
        try:
            world.service.prepare_workspace(run["run_id"])
            outcomes.append("ok")
        except AssignmentDenied as denied:
            outcomes.append(denied.reason)

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("ok") == 1 and outcomes.count("invalid_state") == 3


# ---- persistence and audit ----------------------------------------------------------------


def test_runs_pr_correlation_and_audit_survive_a_restart(tmp_path):
    world = make_world(tmp_path)
    run = pushed_run(world)
    world.service.open_draft_pr(run["run_id"], title="T", summary="s", citations=CITE)
    reopened = AssignmentService(
        policy=world.policy, store=RunStore(tmp_path / "state" / "kepler.json"),
        github=world.github, workspaces_root=world.root, runner=Runner(token=TOKEN),
    )
    view = reopened.get(run["run_id"])
    assert view["state"] == "draft_pr_open" and view["pr_number"] == world.github.pulls[0]["number"]
    assert reason(reopened.authorize, 128, actor="founder") == ("active_run_exists", 409)
    verify_chain(reopened.store.events())


def test_a_corrupt_store_stops_the_service_instead_of_forgetting_runs(tmp_path):
    path = tmp_path / "kepler.json"
    path.write_text("{ not json")
    with pytest.raises(StoreError):
        RunStore(path)
    path.write_text(json.dumps({"runs": {}}))
    with pytest.raises(StoreError):
        RunStore(path)


def test_audit_events_cover_the_lifecycle_and_form_an_intact_chain(tmp_path):
    world = make_world(tmp_path)
    run = pushed_run(world)
    world.service.open_draft_pr(run["run_id"], title="T", summary="s", citations=CITE)
    world.service.cancel(run["run_id"], actor="founder")
    events = world.store.events()
    names = [e["event"] for e in events]
    assert names == ["assignment_authorized", "workspace_started", "workspace_ready",
                     "commit_created", "branch_pushed", "draft_pr_created",
                     "assignment_cancelled"]
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert all(e["agent"] == "kepler" and e["run_id"] == run["run_id"] for e in events)
    verify_chain(events)


def test_tampering_with_the_audit_chain_is_detected(tmp_path):
    world = make_world(tmp_path)
    ready_run(world)
    events = world.store.events()
    verify_chain(events)
    edited = json.loads(json.dumps(events))
    edited[1]["decision"] = "deny"
    with pytest.raises(AuditIntegrityError):
        verify_chain(edited)
    with pytest.raises(AuditIntegrityError):
        verify_chain(events[1:])  # a removed head
    with pytest.raises(AuditIntegrityError):
        verify_chain([events[0], events[2], *events[3:]])  # a removed middle event
    with pytest.raises(AuditIntegrityError):
        verify_chain([events[1], events[0], *events[2:]])  # reordered


def test_unknown_audit_fields_are_rejected():
    with pytest.raises(ValueError):
        build_event(None, timestamp="t", event="e", decision="allow", fields={"token": "x"})


def test_denials_are_audited_too(tmp_path):
    world = make_world(tmp_path)
    reason(world.service.authorize, 128, actor="intruder", request_id="req-9")
    (event,) = world.store.events()
    assert event["event"] == "action_denied" and event["decision"] == "deny"
    assert event["reason"] == "not_authorized" and event["actor"] == "intruder"
    verify_chain([event])


def test_no_secret_reaches_the_store_or_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    world = make_world(tmp_path)
    run = pushed_run(world)
    world.service.open_draft_pr(run["run_id"], title="T", summary="s", citations=CITE)
    reason(world.service.authorize, 128, actor="intruder")
    blob = json.dumps(world.store.data) + caplog.text
    assert TOKEN not in blob and "x-access-token" not in blob
    assert "Authorization" not in blob


def test_git_receives_the_token_only_through_the_environment(monkeypatch, tmp_path):
    import subprocess

    seen: list[tuple[list[str], dict]] = []
    real = subprocess.run

    def spy(argv, **kwargs):
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return real(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    world = make_world(tmp_path)  # clone_url is a local path, so no header is attached
    ready_run(world)
    runner = Runner(token=TOKEN)
    env = runner._git_env("https://github.com/vchokshi/viharbot.git", authenticated=True)
    assert "GIT_CONFIG_VALUE_0" in env and TOKEN not in env["GIT_CONFIG_VALUE_0"]  # base64, not raw
    assert runner._git_env("/local/path", authenticated=True).get("GIT_CONFIG_COUNT") is None
    assert runner._git_env("https://github.com/x/y.git", authenticated=False).get(
        "GIT_CONFIG_COUNT") is None
    assert not any(TOKEN in part for argv, _ in seen for part in argv)  # never in argv
    assert all("GIT_CONFIG_GLOBAL" in e for argv, e in seen if argv[0] == "git")


def test_the_package_makes_no_model_calls():
    package = Path(__file__).resolve().parent.parent / "src" / "viharbot" / "kepler"
    banned = ("litellm", "openai", "anthropic", "strands", "boto3", "bedrock")
    for source in package.glob("*.py"):
        text = source.read_text().lower()
        assert not [b for b in banned if f"import {b}" in text or f"from {b}" in text], source
