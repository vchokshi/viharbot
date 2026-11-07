"""Scoped, read-only access to the restricted instructor repository (epic #139, issue #141).

`InstructorReader` is the only code that reads `vchokshi/viharbot-onboarding-instructor`. It is
deliberately narrow:

- **Read-only.** It issues GET requests only, with its own token (`ARCHIE_INSTRUCTOR_READ_TOKEN`,
  separate from the proposal-writing token) and refuses any repository but the configured one.
- **Assignment required.** Every read names an active founder assignment (issue #140); a cancelled,
  completed or unknown assignment is refused before any network call.
- **Allowlist plus hard deny.** Only paths under `ALLOWED_PREFIXES` are readable. Private
  acknowledgment records, credentials and hidden files are never readable, whatever the prefix.
- **Confidential by default-deny.** Rubrics, answer keys, defect catalogs and assessments are
  readable only when the reader is built with `allow_confidential=True`. Their text is fingerprinted
  into a `ConfidentialIndex` so `assert_trainee_safe` can reject output that repeats it.
- **Symlinks are refused.** The parent listing must show a regular file at the exact path and the
  fetched content must carry the same path and blob sha, so a link cannot smuggle in its target.
- **No content in logs.** Log records carry a path hash, sensitivity and outcome, never text.

Limitation: the leak check matches normalised whole lines of at least `MIN_LINE_CHARS` characters.
It catches copying, not paraphrase, so a human still reviews anything bound for Ada (#144).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from enum import StrEnum

from ..sage_review_github import GitHubClient, GitHubError
from .assignment import AssignmentStore

logger = logging.getLogger("archie-learning")

ALLOWED_PREFIXES = ("knowledge/", "curriculum/", "lessons/", "rubrics/", "answer-keys/", "defects/",
                    "assessments/")
# Matched against the whole lower-cased path; a hit is a hard deny.
NEVER_READABLE = re.compile(r"acknowledg|credential|secret|token|password|\.env|\.pem|\.key$|id_rsa")
CONFIDENTIAL = re.compile(r"rubric|answer|defect|assessment|grading|\bkey")
MAX_PATH_CHARS = 200
MAX_BYTES = 200_000
MIN_LINE_CHARS = 24
_SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class AccessDenied(PermissionError):
    """The path or sensitivity class is outside what this reader may read."""


class ConfidentialLeak(ValueError):
    """Text intended for a trainee-facing audience repeats confidential instructor content."""


class Sensitivity(StrEnum):
    GENERAL = "general"
    CONFIDENTIAL = "confidential"


def path_hash(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:12]


def classify(path: str) -> Sensitivity:
    return Sensitivity.CONFIDENTIAL if CONFIDENTIAL.search(path.lower()) else Sensitivity.GENERAL


def check_path(path: str) -> str:
    """Validate and normalise a repository path, or raise AccessDenied. Never reveals the path."""
    if (not path or len(path) > MAX_PATH_CHARS or not _SAFE_PATH.match(path)
            or ".." in path or "//" in path or any(seg.startswith(".") for seg in path.split("/"))):
        raise AccessDenied("path is not a permitted instructor-repository path")
    lowered = path.lower()
    if NEVER_READABLE.search(lowered):
        raise AccessDenied("this class of instructor-repository content is never readable")
    if not lowered.startswith(ALLOWED_PREFIXES):
        raise AccessDenied("path is outside the instructor-repository allowlist")
    return path


def _lines(text: str) -> set[str]:
    out = set()
    for line in text.splitlines():
        norm = " ".join(line.casefold().split())
        if len(norm) >= MIN_LINE_CHARS:
            out.add(hashlib.sha256(norm.encode()).hexdigest())
    return out


class ConfidentialIndex:
    """In-memory fingerprints of confidential text. Holds hashes only and is never persisted."""

    def __init__(self) -> None:
        self._hashes: set[str] = set()

    def add(self, text: str) -> None:
        self._hashes |= _lines(text)

    def leaks(self, text: str) -> bool:
        return bool(_lines(text) & self._hashes)

    def assert_trainee_safe(self, text: str) -> None:
        if self.leaks(text):
            raise ConfidentialLeak("text repeats confidential instructor content")


class InstructorDocument:
    """One file read from the instructor repository. `repr` never shows the text."""

    __slots__ = ("path", "blob_sha", "text", "sensitivity")

    def __init__(self, path: str, blob_sha: str, text: str, sensitivity: Sensitivity) -> None:
        self.path, self.blob_sha, self.text, self.sensitivity = path, blob_sha, text, sensitivity

    def __repr__(self) -> str:
        return f"InstructorDocument(path_hash={path_hash(self.path)}, {self.sensitivity.value})"


class InstructorReader:
    def __init__(self, client: GitHubClient, repository: str, assignments: AssignmentStore,
                 allow_confidential: bool = False, index: ConfidentialIndex | None = None) -> None:
        self._client = client
        self._repo = repository
        self._assignments = assignments
        self._allow_confidential = allow_confidential
        self.index = index if index is not None else ConfidentialIndex()

    def read(self, assignment_id: str, path: str, repo: str | None = None,
             request_id: str = "-") -> InstructorDocument:
        started, outcome = time.monotonic(), "ok"
        sensitivity = Sensitivity.GENERAL
        try:
            self._assignments.require_active(assignment_id)
            if (repo or self._repo) != self._repo:
                raise AccessDenied("repository is not the configured one")
            path = check_path(path)
            sensitivity = classify(path)
            if sensitivity is Sensitivity.CONFIDENTIAL and not self._allow_confidential:
                raise AccessDenied("confidential instructor content is not enabled for this reader")
            # GitHub resolves an in-repository symlink to its target's content, which would bypass the
            # path rules above. So first require, from the parent listing, a regular-file entry at
            # exactly this path, and afterwards that the response is that same entry.
            entry = self._regular_file_entry(path)
            doc = self._client.request("GET", f"/repos/{self._repo}/contents/{path}")
            if (not isinstance(doc, dict) or doc.get("type") != "file" or doc.get("path") != path
                    or doc.get("sha") != entry.get("sha")):
                raise AccessDenied("path is not a single regular file")
            if int(doc.get("size", 0)) > MAX_BYTES:
                raise AccessDenied("file exceeds the size limit")
            text = base64.b64decode(doc.get("content", "")).decode("utf-8")
            if sensitivity is Sensitivity.CONFIDENTIAL:
                self.index.add(text)
            return InstructorDocument(path, str(doc.get("sha", "")), text, sensitivity)
        except Exception as exc:
            outcome = type(exc).__name__
            raise
        finally:
            logger.info("instructor_read", extra={
                "agent": "archie", "request_id": request_id, "destination_agent": "instructor-repo",
                "model_alias": None, "duration_ms": int((time.monotonic() - started) * 1000),
                "path_hash": path_hash(path), "sensitivity": sensitivity.value,
                **({"error": outcome} if outcome != "ok" else {})})

    def _regular_file_entry(self, path: str) -> dict:
        parent = path.rpartition("/")[0]
        rows = self._client.request("GET", f"/repos/{self._repo}/contents/{parent}")
        if not isinstance(rows, list):
            raise AccessDenied("path is not a single regular file")
        for row in rows:
            if row.get("path") == path:
                if row.get("type") != "file":  # symlink, submodule or directory
                    raise AccessDenied("path is not a single regular file")
                return row
        raise AccessDenied("path is not a single regular file")

    def list(self, assignment_id: str, directory: str) -> list[str]:
        """Readable file paths directly under an allowlisted directory; others are omitted."""
        self._assignments.require_active(assignment_id)
        directory = check_path(directory.rstrip("/") + "/x")[:-1].rstrip("/")
        rows = self._client.request("GET", f"/repos/{self._repo}/contents/{directory}")
        if not isinstance(rows, list):
            raise GitHubError("not a directory")
        readable = []
        for row in rows:
            if row.get("type") != "file":
                continue
            try:
                candidate = check_path(str(row.get("path", "")))
                if not candidate.startswith(directory + "/"):  # a symlinked directory lists its target
                    continue
            except AccessDenied:
                continue
            if classify(candidate) is Sensitivity.CONFIDENTIAL and not self._allow_confidential:
                continue
            readable.append(candidate)
        return readable
