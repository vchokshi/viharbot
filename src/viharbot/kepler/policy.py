"""Kepler's assignment policy: who may assign work, which repository, which branches.

Everything here comes from trusted configuration, never from a request. A request names only an
issue number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class AssignmentDenied(Exception):
    """An action is refused. `reason` is a fixed code that is safe to log and return."""

    def __init__(self, reason: str, status: int = 422) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


# Each part must start with an alphanumeric or underscore, so "." and ".." are never components.
_REPOSITORY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_BRANCH_PREFIX = re.compile(r"^[a-z0-9][a-z0-9_-]*/$")
# Kepler never touches this repository, in any role, whatever the configuration says.
FORBIDDEN_REPOSITORIES = frozenset({"vchokshi/viharbot-onboarding-instructor"})
NEVER_PUSH_BRANCHES = frozenset({"main", "master", "head"})
DEFAULT_TEST_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("python", "-m", "pytest"),
    ("ruff", "check"),
)


@dataclass(frozen=True)
class KeplerPolicy:
    repository: str = "vchokshi/viharbot"
    assigners: frozenset[str] = frozenset({"founder"})  # service_auth registry subjects
    base_branch: str = "main"
    branch_prefix: str = "kepler/"
    # Where to clone from. Defaults to the repository on GitHub. Set only in trusted
    # configuration (tests use a local path); a request can never supply it.
    clone_url: str | None = None
    test_commands: tuple[tuple[str, ...], ...] = DEFAULT_TEST_COMMANDS
    command_timeout_seconds: int = 900

    def __post_init__(self) -> None:
        if not _REPOSITORY.fullmatch(self.repository):
            raise ValueError("repository must be owner/name")
        if self.repository.casefold() in {r.casefold() for r in FORBIDDEN_REPOSITORIES}:
            raise ValueError("this repository is never accessible to Kepler")
        if not _BRANCH_PREFIX.fullmatch(self.branch_prefix):
            raise ValueError("branch_prefix must look like 'kepler/'")
        if not self.assigners:
            raise ValueError("at least one assigner is required")
        if not all(isinstance(c, tuple) and c for c in self.test_commands):
            raise ValueError("test_commands must be non-empty argument tuples")
        if not 1 <= self.command_timeout_seconds <= 3600:
            raise ValueError("command_timeout_seconds must be between 1 and 3600")

    @property
    def source(self) -> str:
        return self.clone_url or f"https://github.com/{self.repository}.git"

    def branch_for(self, issue: int) -> str:
        """The only branch Kepler may push for an issue, derived from the issue number alone."""
        return f"{self.branch_prefix}issue-{issue}"

    def check_branch(self, branch: str, issue: int) -> None:
        """Raise unless `branch` is exactly the feature branch for `issue`."""
        if (
            branch != self.branch_for(issue)
            or branch.casefold() in NEVER_PUSH_BRANCHES
            or branch == self.base_branch
        ):
            raise AssignmentDenied("branch_not_allowed", 403)

    def command_allowed(self, argv: list[str]) -> bool:
        """An argv is allowed only if it starts with a configured command.

        `python` and `ruff` resolve through PATH; tests may configure an absolute interpreter.
        """
        return any(tuple(argv[: len(c)]) == c for c in self.test_commands)
