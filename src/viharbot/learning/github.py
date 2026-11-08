"""GitHub access for the learning pipeline, split by least privilege.

`SourceReader` reads a merged pull request in the one configured source repository. It has no
write call. `InstructorProposer` writes to the one configured instructor repository: it can create
a branch, add one file and open a pull request. Neither class has a call that merges, approves,
closes, deletes or changes settings, and each refuses any repository other than its own.
Every response is treated as untrusted data.
"""

from __future__ import annotations

import base64
import re
import tomllib
from datetime import datetime
from typing import Any

from ..sage_review_format import MARKER
from ..sage_review_github import GitHubClient, GitHubError
from .models import ChangedPath, PRSnapshot, StudentPRSnapshot
from .sage_evidence import DEFAULT_SAGE_AUTHORS, is_sage_author, latest_sage_review

MAX_FILES = 300
MAX_COMMENTS = 20
MAX_COMMENT_CHARS = 1500
MAX_BODY_CHARS = 20_000


class RepositoryNotAllowed(GitHubError):
    pass


def _check(allowed: str, repo: str) -> None:
    if repo != allowed:
        raise RepositoryNotAllowed("repository is not the configured one")


class SourceReader:
    def __init__(self, client: GitHubClient, repository: str,
                 sage_authors: frozenset[str] = DEFAULT_SAGE_AUTHORS) -> None:
        self._client = client
        self._repo = repository
        self._sage_authors = sage_authors

    @property
    def repository(self) -> str:
        return self._repo

    def merged_pull_requests(self, limit: int = 30) -> list[dict[str, Any]]:
        """Recently closed pull requests that were merged, newest first."""
        rows = self._client.request(
            "GET", f"/repos/{self._repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100"
        )
        merged = [r for r in rows if r.get("merged_at") and r.get("merge_commit_sha")]
        return merged[:limit]

    def snapshot(self, number: int, repo: str | None = None) -> PRSnapshot:
        repo = repo or self._repo
        _check(self._repo, repo)
        pr = self._client.request("GET", f"/repos/{repo}/pulls/{number}")
        if not pr.get("merged_at") or not pr.get("merge_commit_sha"):
            raise GitHubError("pull request is not merged")
        raw_files = self._client.pages(f"/repos/{repo}/pulls/{number}/files", MAX_FILES + 1)
        files = tuple(
            ChangedPath(path=f["filename"], status=f.get("status", "modified"),
                        additions=f.get("additions", 0), deletions=f.get("deletions", 0))
            for f in raw_files[:MAX_FILES]
        )
        commits = self._client.pages(f"/repos/{repo}/pulls/{number}/commits", 100)
        subjects = tuple((c["commit"]["message"].splitlines() or [""])[0][:200] for c in commits)
        comments: list[str] = []
        issue_comments: list[dict[str, Any]] = []
        for path in (f"/repos/{repo}/pulls/{number}/comments", f"/repos/{repo}/issues/{number}/comments"):
            for c in self._client.pages(path, MAX_COMMENTS):
                if path.endswith(f"/issues/{number}/comments"):
                    issue_comments.append(c)
                if is_sage_author(c, self._sage_authors) and MARKER in (c.get("body") or ""):
                    continue  # a trusted Sage review is parsed below, not quoted as discussion
                text = (c.get("body") or "").strip()
                if text and len(comments) < MAX_COMMENTS:
                    comments.append(text[:MAX_COMMENT_CHARS])
        head = (pr.get("head") or {}).get("sha")
        head_sha = head if isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head) else None
        return PRSnapshot(
            repository=repo, number=number, title=(pr.get("title") or "")[:300],
            body=(pr.get("body") or "")[:MAX_BODY_CHARS],
            merged_at=datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00")),
            merge_commit_sha=pr["merge_commit_sha"], base_ref=pr["base"]["ref"], files=files,
            commit_subjects=subjects, discussion=tuple(comments),
            software_version=self._version(repo, pr["merge_commit_sha"]),
            files_omitted=max(0, len(raw_files) - MAX_FILES), head_sha=head_sha,
            sage_review=latest_sage_review(issue_comments, head_sha, self._sage_authors),
        )

    def review_snapshot(self, number: int, repo: str | None = None) -> StudentPRSnapshot:
        """The pull request as it is now, open or merged, with its newest trusted Sage review.

        Read-only. The result names the head commit, so feedback built from it is tied to that exact
        revision; `sage_review.reviewed_head_current` says whether Sage reviewed that same commit."""
        repo = repo or self._repo
        _check(self._repo, repo)
        pr = self._client.request("GET", f"/repos/{repo}/pulls/{number}")
        head = (pr.get("head") or {}).get("sha")
        if not (isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head)):
            raise GitHubError("pull request head commit is unavailable")
        raw_files = self._client.pages(f"/repos/{repo}/pulls/{number}/files", MAX_FILES + 1)
        files = tuple(ChangedPath(path=f["filename"], status=f.get("status", "modified"),
                                  additions=f.get("additions", 0), deletions=f.get("deletions", 0))
                      for f in raw_files[:MAX_FILES])
        issue_comments = self._client.pages(f"/repos/{repo}/issues/{number}/comments", MAX_COMMENTS)
        return StudentPRSnapshot(
            repository=repo, number=number, author_login=str((pr.get("user") or {}).get("login", "")),
            state="open" if pr.get("state") == "open" else "closed", title=(pr.get("title") or "")[:300],
            body=(pr.get("body") or "")[:MAX_BODY_CHARS], head_sha=head, base_ref=(pr.get("base") or {}).get("ref", ""),
            files=files, files_omitted=max(0, len(raw_files) - MAX_FILES),
            sage_review=latest_sage_review(issue_comments, head, self._sage_authors))

    def _version(self, repo: str, sha: str) -> str:
        try:
            doc = self._client.request("GET", f"/repos/{repo}/contents/pyproject.toml?ref={sha}")
            text = base64.b64decode(doc["content"]).decode("utf-8")
            return str(tomllib.loads(text)["project"]["version"])
        except Exception:  # version is optional metadata; absence is recorded as "unknown"
            return "unknown"


class InstructorProposer:
    def __init__(self, client: GitHubClient, repository: str) -> None:
        self._client = client
        self._repo = repository

    def propose(self, path: str, content: str, branch: str, title: str, body: str,
                repo: str | None = None) -> str:
        """Open a pull request that adds one file. Returns its URL. Never merges.

        Idempotent: if the branch or an open pull request for it already exists, that pull
        request's URL is returned and nothing is created twice.
        """
        repo = repo or self._repo
        _check(self._repo, repo)
        owner = repo.split("/")[0]
        existing = self._client.request("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}")
        if existing:
            return str(existing[0]["html_url"])
        default = self._client.request("GET", f"/repos/{repo}")["default_branch"]
        base_sha = self._client.request("GET", f"/repos/{repo}/git/ref/heads/{default}")["object"]["sha"]
        try:
            self._client.request("POST", f"/repos/{repo}/git/refs",
                                 {"ref": f"refs/heads/{branch}", "sha": base_sha})
        except GitHubError as exc:
            if "422" not in str(exc):  # 422: the branch already exists from an earlier attempt
                raise
        try:
            self._client.request("GET", f"/repos/{repo}/contents/{path}?ref={branch}")
        except GitHubError:
            self._client.request("PUT", f"/repos/{repo}/contents/{path}", {
                "message": title, "branch": branch,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            })
        pr = self._client.request("POST", f"/repos/{repo}/pulls",
                                  {"title": title, "head": branch, "base": default, "body": body})
        return str(pr["html_url"])
