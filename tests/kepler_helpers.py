"""Offline builders for the Kepler assignment tests: a local bare repo as the 'remote', a fake
GitHub client and throwaway state. Real git, no network, no model calls."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from viharbot.kepler.github import GitHubError, IssueInfo, PullRequest
from viharbot.kepler.policy import KeplerPolicy
from viharbot.kepler.service import AssignmentService
from viharbot.kepler.store import RunStore
from viharbot.kepler.workspace import Runner
from viharbot.kepler_governance import REQUIRED_DOCUMENTS

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "ghp_SECRETTOKENVALUE0123456789abcdef"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


def git(*args: str, cwd: Path | None = None) -> str:
    import os

    done = subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True, text=True,
        check=True,
    )
    return done.stdout.strip()


def make_remote(tmp_path: Path, *, with_governance: bool = True) -> Path:
    """A bare repository with `main` holding the governance documents and a small source file."""
    work = tmp_path / "seed"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    if with_governance:
        for doc in REQUIRED_DOCUMENTS:
            target = work / doc.path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / doc.path, target)
    (work / "AGENTS.md").write_text("# Agents\n")
    (work / "src").mkdir()
    (work / "src" / "app.py").write_text("print('hello')\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "seed", cwd=work)
    bare = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(work), str(bare))
    return bare


@dataclass
class FakeGitHub:
    issues: dict[int, IssueInfo] = field(default_factory=lambda: {
        128: IssueInfo(128, "open", "Manual assignment", False),
        129: IssueInfo(129, "open", "Another", False),
        50: IssueInfo(50, "open", "A pull request", True),
        51: IssueInfo(51, "closed", "Closed", False),
    })
    pulls: list[dict] = field(default_factory=list)
    calls: list[tuple] = field(default_factory=list)
    fail: bool = False

    def get_issue(self, number):
        self.calls.append(("get_issue", number))
        if self.fail:
            raise GitHubError("GitHub request failed")
        return self.issues.get(number)

    def find_pull_request(self, branch):
        self.calls.append(("find_pull_request", branch))
        for pull in self.pulls:
            if pull["head"] == branch:
                return PullRequest(pull["number"], pull["url"], True, branch)
        return None

    def create_draft_pull_request(self, branch, title, body):
        self.calls.append(("create", branch, title))
        number = 900 + len(self.pulls)
        pull = {"number": number, "url": f"https://example.invalid/pull/{number}", "head": branch,
                "title": title, "body": body, "draft": True}
        self.pulls.append(pull)
        return PullRequest(number, pull["url"], True, branch)

    def update_pull_request(self, number, title, body):
        self.calls.append(("update", number, title))
        for pull in self.pulls:
            if pull["number"] == number:
                pull.update(title=title, body=body)
                return PullRequest(number, pull["url"], True, pull["head"])
        raise GitHubError("GitHub returned HTTP 404")


def make_world(tmp_path: Path, *, with_governance: bool = True, **policy_kwargs) -> SimpleNamespace:
    remote = make_remote(tmp_path, with_governance=with_governance)
    policy = KeplerPolicy(
        assigners=frozenset({"founder"}), clone_url=str(remote), **policy_kwargs
    )
    store = RunStore(tmp_path / "state" / "kepler.json") if (tmp_path / "state").is_dir() else None
    if store is None:
        (tmp_path / "state").mkdir()
        store = RunStore(tmp_path / "state" / "kepler.json")
    github = FakeGitHub()
    service = AssignmentService(
        policy=policy, store=store, github=github, workspaces_root=tmp_path / "workspaces",
        runner=Runner(token=TOKEN, timeout=60),
    )
    return SimpleNamespace(
        remote=remote, policy=policy, store=store, github=github, service=service,
        root=tmp_path / "workspaces", tmp=tmp_path,
    )


def ready_run(world, issue: int = 128) -> dict:
    run = world.service.authorize(issue, actor="founder", request_id="r1")
    world.service.prepare_workspace(run["run_id"])
    return run


def workspace_of(world, run) -> Path:
    return Path(world.store.get_run(run["run_id"])["workspace"])
