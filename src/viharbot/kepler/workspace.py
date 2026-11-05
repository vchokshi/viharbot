"""Per-issue git workspaces and the narrow git/command surface Kepler may use.

Git runs with the user and system config ignored, hooks disabled and prompts off. The GitHub token
is passed to git only through the process environment (never in argv, where `ps` would show it),
only for clone and push, and never to approved test commands. Output that is stored or raised is
scrubbed of the token.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .policy import AssignmentDenied, KeplerPolicy

MAX_OUTPUT_TAIL = 2000


class GitError(Exception):
    """A git command failed. The message is scrubbed and never contains the token."""


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    duration_ms: int
    output_sha256: str
    tail: str


class Runner:
    """Runs git and approved commands with a minimal, token-aware environment."""

    def __init__(self, token: str | None = None, timeout: int = 900) -> None:
        self._token = token.strip() if token else None
        self._timeout = timeout

    def _base_env(self) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.devnull if os.name == "posix" else "",
            "LC_ALL": "C",
        }
        return {k: v for k, v in env.items() if v}

    def _git_env(self, source: str, *, authenticated: bool) -> dict[str, str]:
        env = self._base_env()
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
        )
        if authenticated and self._token and source.startswith("https://"):
            pair = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                GIT_CONFIG_VALUE_0=f"Authorization: basic {pair}",
            )
        return env

    def scrub(self, text: str) -> str:
        if self._token:
            pair = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            text = text.replace(self._token, "***").replace(pair, "***")
        return text

    def git(
        self, args: list[str], cwd: Path | None, *, source: str = "", authenticated: bool = False
    ) -> str:
        argv = [
            "git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
            "-c", "user.name=Kepler", "-c", "user.email=kepler@viharbot.invalid",
            "-c", "commit.gpgsign=false", *args,
        ]
        try:
            done = subprocess.run(
                argv, cwd=cwd, env=self._git_env(source, authenticated=authenticated),
                capture_output=True, text=True, timeout=self._timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise GitError(f"git {args[0]} could not run") from None
        if done.returncode != 0:
            detail = self.scrub((done.stderr or "").strip())[-300:]
            raise GitError(f"git {args[0]} failed (exit {done.returncode}): {detail}")
        return done.stdout

    def run_command(self, argv: list[str], cwd: Path) -> CommandResult:
        """Run an already-approved command in the workspace without any GitHub credential."""
        started = time.monotonic()
        try:
            done = subprocess.run(
                argv, cwd=cwd, env=self._base_env(), capture_output=True, text=True,
                timeout=self._timeout, check=False,
            )
            code, output = done.returncode, (done.stdout or "") + (done.stderr or "")
        except subprocess.TimeoutExpired:
            code, output = 124, "command timed out"
        except OSError:
            code, output = 127, "command could not run"
        output = self.scrub(output)
        return CommandResult(
            exit_code=code,
            duration_ms=int((time.monotonic() - started) * 1000),
            output_sha256=hashlib.sha256(output.encode("utf-8", "replace")).hexdigest(),
            tail=output[-MAX_OUTPUT_TAIL:],
        )


class Workspace:
    """One issue's isolated checkout. The path is always directly under the workspaces root."""

    def __init__(self, root: Path, name: str, policy: KeplerPolicy, runner: Runner) -> None:
        self.root = Path(root).resolve()
        self.path = (self.root / name).resolve()
        if self.path.parent != self.root:
            raise AssignmentDenied("workspace_path_invalid", 500)
        self._policy = policy
        self._runner = runner

    def clone(self, branch: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise AssignmentDenied("workspace_exists", 409)  # never reuse another run's files
        source = self._policy.source
        self._runner.git(
            ["clone", "--no-tags", "--single-branch", "--branch", self._policy.base_branch,
             "--", source, str(self.path)],
            None, source=source, authenticated=True,
        )
        self._runner.git(["checkout", "-b", branch], self.path)

    def stage_all(self) -> list[str]:
        """Stage everything and return the changed paths (renames reported as delete plus add)."""
        self._runner.git(["add", "-A"], self.path)
        out = self._runner.git(
            ["diff", "--cached", "--name-only", "-z", "--no-renames"], self.path
        )
        return [p for p in out.split("\0") if p]

    def unstage_all(self) -> None:
        self._runner.git(["reset", "-q"], self.path)

    def commit(self, message: str) -> str:
        self._runner.git(["commit", "-q", "-m", message], self.path)
        return self._runner.git(["rev-parse", "HEAD"], self.path).strip()

    def commits_ahead(self) -> int:
        base = f"origin/{self._policy.base_branch}"
        return int(self._runner.git(["rev-list", "--count", f"{base}..HEAD"], self.path).strip())

    def push(self, branch: str, issue: int) -> None:
        """Push HEAD to the issue's feature branch only. Never forced, never anything else."""
        self._policy.check_branch(branch, issue)
        source = self._policy.source
        self._runner.git(
            ["push", "--", "origin", f"HEAD:refs/heads/{branch}"],
            self.path, source=source, authenticated=True,
        )

    def remove(self) -> None:
        if self.path.parent == self.root and self.path.exists():
            shutil.rmtree(self.path)
