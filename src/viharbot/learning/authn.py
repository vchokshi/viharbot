"""Authentication boundary for Archie's human-gated operations (epic #139, issues #140, #144, #151).

Before this module a "founder" was a login string typed by whoever ran the CLI. That is not a safe
base for an API. Here a human is a `HumanActor`, and the only way to obtain one is
`CredentialAuthenticator.authenticate(login, token)`, which checks the token against a stored
SHA-256 digest in constant time. Authorization code (`AssignmentStore`, `PublicationService`) accepts
a `HumanActor` and checks its role; an `AgentActor` can never be promoted to one.

Configuration (environment; digests only, never the tokens):
    ARCHIE_FOUNDER_TOKEN_HASHES   "login:sha256hex,login2:sha256hex"
    ARCHIE_REVIEWER_TOKEN_HASHES  same format; reviewers may approve but never publish or assign

Limitations, stated plainly: this authenticates a bearer token held by a person; it does not bind the
token to a device, rotate it, or protect it in transit. The mint guard stops accidental or
agent-driven construction of an actor; it does not stop code already running inside this process
from importing the private mint, so callers must be trusted in-process code (a future API layer
authenticates each request here and passes the actor on). Failed attempts lock a login out for a
period so a token cannot be guessed freely.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
MIN_TOKEN_CHARS = 24
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300.0
_MINT = object()


class AuthenticationFailed(PermissionError):
    """The credentials were wrong, the login is unknown, or the login is locked out."""


class Unauthorized(PermissionError):
    """An authenticated actor lacks the role or relationship this operation requires."""


class Role(StrEnum):
    FOUNDER = "founder"
    REVIEWER = "reviewer"


@dataclass(frozen=True)
class HumanActor:
    """A person who proved a credential. Not constructible outside this module's authenticator."""

    login: str
    role: Role
    _mint: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise Unauthorized("a HumanActor can only be created by authenticating")


@dataclass(frozen=True)
class AgentActor:
    """An agent identity acting inside its own authority (for example Archie submitting a draft).

    It carries no permission by itself; each operation names what an agent may do."""

    agent_id: str

    @property
    def login(self) -> str:
        return f"agent:{self.agent_id}"


Actor = HumanActor | AgentActor
ARCHIE = AgentActor("archie")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(","):
        login, _, digest = part.strip().partition(":")
        if _LOGIN.match(login) and _HEX.match(digest.lower()):
            out[login.lower()] = digest.lower()
    return out


class CredentialAuthenticator:
    def __init__(self, founders: dict[str, str], reviewers: dict[str, str] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._digests: dict[str, tuple[Role, str]] = {}
        for login, digest in (reviewers or {}).items():
            self._digests[login.lower()] = (Role.REVIEWER, digest)
        for login, digest in founders.items():  # a login in both lists is a founder
            self._digests[login.lower()] = (Role.FOUNDER, digest)
        self._clock = clock
        self._failures: dict[str, tuple[int, float]] = {}

    @classmethod
    def from_env(cls) -> "CredentialAuthenticator":
        return cls(_parse(os.getenv("ARCHIE_FOUNDER_TOKEN_HASHES", "")),
                   _parse(os.getenv("ARCHIE_REVIEWER_TOKEN_HASHES", "")))

    @property
    def configured(self) -> bool:
        return bool(self._digests)

    def authenticate(self, login: str, token: str) -> HumanActor:
        key = login.strip().lower()
        count, since = self._failures.get(key, (0, 0.0))
        if count >= MAX_FAILURES and self._clock() - since < LOCKOUT_SECONDS:
            raise AuthenticationFailed("too many failed attempts; try later")
        entry = self._digests.get(key)
        # Always compare something, so an unknown login costs the same as a wrong token.
        expected = entry[1] if entry else "0" * 64
        ok = (hmac.compare_digest(hash_token(token), expected) and entry is not None
              and len(token) >= MIN_TOKEN_CHARS)
        if not ok:
            self._failures[key] = (count + 1 if self._clock() - since < LOCKOUT_SECONDS else 1,
                                   self._clock())
            raise AuthenticationFailed("authentication failed")
        self._failures.pop(key, None)
        return HumanActor(login=key, role=entry[0], _mint=_MINT)
