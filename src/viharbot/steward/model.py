"""Plain data for an assessment. Fixed wording only: no contributor-written text is ever stored."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

DONE, OUTSTANDING, NEEDS_HUMAN, INFO = "done", "outstanding", "needs_human", "info"
STATUSES = (DONE, OUTSTANDING, NEEDS_HUMAN, INFO)
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


def safe_login(login: str | None) -> str | None:
    """A GitHub login only if it has GitHub's own shape (letters, digits, hyphens), else None.
    Used wherever a login is placed in an email or a draft, so nothing else can slip in."""
    return login if isinstance(login, str) and _LOGIN.fullmatch(login) else None


@dataclass(frozen=True)
class Requirement:
    key: str
    title: str
    status: str
    detail: str  # fixed wording chosen by code


@dataclass(frozen=True)
class Observation:
    """One thing worth telling the founder about. The id is what deduplicates alerts."""

    observation_id: str
    requirement: str
    status: str
    summary: str


@dataclass(frozen=True)
class PullAssessment:
    repo: str
    number: int
    author_login: str
    author_id: int
    head_sha: str
    first_time: bool
    is_fork: bool
    requirements: tuple[Requirement, ...]
    blockers: tuple[str, ...]
    recommended_actions: tuple[str, ...]
    observations: tuple[Observation, ...]
    gate_sha256: str  # fingerprint of the CLA gate code that judged this

    def requirement(self, key: str) -> Requirement:
        for item in self.requirements:
            if item.key == key:
                return item
        raise KeyError(key)

    def to_dict(self) -> dict:
        return asdict(self)
