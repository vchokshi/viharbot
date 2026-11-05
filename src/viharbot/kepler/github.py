"""The only GitHub operations Kepler can perform, behind a narrow interface.

There is deliberately no method to merge, close, delete, approve or force-push, so no caller can
do those things through this module. Pull requests are always created as drafts against the
configured base branch. The token is injected, sent only as a request header over HTTPS (or to a
loopback/private address), and never placed in a URL, log line or error message.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.parse import quote, urlsplit

from .policy import KeplerPolicy

API_BASE = "https://api.github.com"
MAX_RESPONSE_BYTES = 2_000_000
REQUEST_TIMEOUT = 20


class GitHubError(Exception):
    """A GitHub call failed. The message never contains the token or response bodies."""


@dataclass(frozen=True)
class IssueInfo:
    number: int
    state: str
    title: str
    is_pull_request: bool


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    draft: bool
    head: str


class GitHubClient(Protocol):
    def get_issue(self, number: int) -> IssueInfo | None: ...
    def find_pull_request(self, branch: str) -> PullRequest | None: ...
    def create_draft_pull_request(self, branch: str, title: str, body: str) -> PullRequest: ...
    def update_pull_request(self, number: int, title: str, body: str) -> PullRequest: ...


def _check_api_base(api_base: str) -> str:
    parts = urlsplit(api_base)
    if parts.scheme == "https" and parts.hostname:
        return api_base.rstrip("/")
    if parts.scheme == "http" and parts.hostname:
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            address = None
        if address is not None and (address.is_loopback or address.is_private):
            return api_base.rstrip("/")
    raise ValueError("api_base must be HTTPS, or HTTP to a loopback or private IP address")


class HttpGitHubClient:
    """GitHub REST client scoped to one repository, using only the standard library."""

    def __init__(
        self,
        policy: KeplerPolicy,
        token: str,
        *,
        api_base: str = API_BASE,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("a GitHub token is required")
        self._policy = policy
        self._token = token.strip()
        self._api = _check_api_base(api_base)
        self._open = opener
        self._repo = f"/repos/{policy.repository}"

    def _request(self, method: str, path: str, payload: dict | None = None) -> object | None:
        if not path.startswith(self._repo + "/") and path != self._repo:
            raise GitHubError("request outside the configured repository")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - scheme validated in _check_api_base
            self._api + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "viharbot-kepler",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with self._open(request, timeout=REQUEST_TIMEOUT) as response:  # type: ignore[call-arg]
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise GitHubError(f"GitHub returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GitHubError("GitHub request failed") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GitHubError("GitHub response too large")
        try:
            return json.loads(raw)
        except ValueError:
            raise GitHubError("GitHub returned invalid JSON") from None

    @staticmethod
    def _pull(item: object) -> PullRequest:
        if not isinstance(item, dict):
            raise GitHubError("unexpected pull request response")
        try:
            return PullRequest(
                number=int(item["number"]),
                url=str(item["html_url"]),
                draft=bool(item["draft"]),
                head=str(item["head"]["ref"]),
            )
        except (KeyError, TypeError, ValueError):
            raise GitHubError("unexpected pull request response") from None

    def get_issue(self, number: int) -> IssueInfo | None:
        item = self._request("GET", f"{self._repo}/issues/{int(number)}")
        if item is None:
            return None
        if not isinstance(item, dict):
            raise GitHubError("unexpected issue response")
        try:
            return IssueInfo(
                number=int(item["number"]),
                state=str(item["state"]),
                title=str(item.get("title", "")),
                is_pull_request="pull_request" in item,
            )
        except (KeyError, TypeError, ValueError):
            raise GitHubError("unexpected issue response") from None

    def find_pull_request(self, branch: str) -> PullRequest | None:
        owner = self._policy.repository.split("/")[0]
        head = quote(f"{owner}:{branch}", safe="")
        items = self._request("GET", f"{self._repo}/pulls?head={head}&state=open")
        if not isinstance(items, list):
            return None
        for item in items:
            pull = self._pull(item)
            if pull.head == branch:
                return pull
        return None

    def create_draft_pull_request(self, branch: str, title: str, body: str) -> PullRequest:
        item = self._request(
            "POST",
            f"{self._repo}/pulls",
            {"title": title, "body": body, "head": branch,
             "base": self._policy.base_branch, "draft": True},
        )
        pull = self._pull(item)
        if not pull.draft:
            raise GitHubError("pull request was not created as a draft")
        return pull

    def update_pull_request(self, number: int, title: str, body: str) -> PullRequest:
        # Only title and body are ever sent: never state, base or draft status.
        item = self._request(
            "PATCH", f"{self._repo}/pulls/{int(number)}", {"title": title, "body": body}
        )
        return self._pull(item)
