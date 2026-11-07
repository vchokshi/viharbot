"""Read Sage's advisory review of a pull request as evidence for Archie (epic #139, issue #142).

Sage posts one comment per review (see `sage_review_format.py`). This module turns that comment
into typed, bounded data. It makes no network call and no model call.

Trust: the hidden markers in a comment are plain text, so anyone who can comment could forge them.
A comment is therefore accepted as Sage's only if its GitHub author is a Bot account on the
configured list (default `github-actions[bot]`, the identity the Sage workflow posts as). Every other
comment, markers or not, stays ordinary untrusted discussion. Even an accepted comment is an AI
opinion: its findings are recorded with provenance and labelled advisory, never as verified fact,
and whether a finding was fixed is not decided here.

Limitation: any workflow in the source repository posts as `github-actions[bot]`, so this proves
"a workflow in this repository wrote it", not "the Sage workflow wrote it". The reviewed-commit
check below shows whether the review still matches the pull request's final head.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..sage_review_format import MARKER, SHA_RE, STATUS_RE
from .models import SageEvidence, SageFinding

DEFAULT_SAGE_AUTHORS: frozenset[str] = frozenset({"github-actions[bot]"})
MAX_FINDINGS = 40
MAX_DESCRIPTION_CHARS = 600
_SECTION_START = "### Recommended changes"
_SECTION_END = "### Architectural implications"
_RECOMMENDATION = re.compile(r"\*\*Recommendation: ([A-Z_ ]{1,40})\*\*")
_FINDING = re.compile(
    r"(?m)^- \*\*(?P<sev>[A-Z]{3,8})\*\* · (?P<cat>[^·\n]{1,60}) · (?P<basis>[^\n`]{1,60}?)"
    r"(?: `(?P<loc>[^`\n]{1,200})`)?\n  (?P<desc>[^\n]*)")
_SEVERITIES = ("critical", "high", "medium", "low", "info")
_ESCAPED = re.compile(r"\\([\\`*_\[\]()#!|~>+\-.])")


def _plain(text: str) -> str:
    """Undo the escaping `sage_review_format.neutralize` applied, so stored text reads naturally."""
    text = _ESCAPED.sub(r"\1", text).replace("​", "")
    return text.replace("&lt;", "<").replace("&amp;", "&")


def is_sage_author(comment: dict[str, Any], trusted: Iterable[str] = DEFAULT_SAGE_AUTHORS) -> bool:
    user = comment.get("user") or {}
    return user.get("type") == "Bot" and user.get("login") in frozenset(trusted)


def parse_sage_comment(comment: dict[str, Any],
                       trusted: Iterable[str] = DEFAULT_SAGE_AUTHORS) -> SageEvidence | None:
    """The evidence in one comment, or None if it is not a well-formed Sage review by a trusted author.

    `reviewed_head_current` is left unknown; `latest_sage_review` fills it in.
    """
    body = comment.get("body") or ""
    if not is_sage_author(comment, trusted) or MARKER not in body:
        return None
    sha, status = SHA_RE.search(body), STATUS_RE.search(body)
    recommendation = _RECOMMENDATION.search(body)
    if not (sha and status and recommendation):
        return None
    start, end = body.find(_SECTION_START), body.find(_SECTION_END)
    section = body[start:end] if 0 <= start < end else ""
    found: list[SageFinding] = []
    total = 0
    for m in _FINDING.finditer(section):
        severity = m["sev"].lower()
        if severity not in _SEVERITIES:
            continue
        total += 1
        if len(found) < MAX_FINDINGS:
            found.append(SageFinding(
                severity=severity, category=_plain(m["cat"]).strip(), basis=_plain(m["basis"]).strip(),
                location=_plain(m["loc"]) if m["loc"] else None,
                description=_plain(m["desc"]).strip()[:MAX_DESCRIPTION_CHARS]))
    return SageEvidence(
        reviewed_sha=sha[1], reviewed_head_current=None, completeness=status[1],
        recommendation=recommendation[1].strip(), findings=tuple(found),
        comment_url=comment.get("html_url") if isinstance(comment.get("html_url"), str) else None,
        findings_omitted=total - len(found))


def latest_sage_review(comments: list[dict[str, Any]], head_sha: str | None,
                       trusted: Iterable[str] = DEFAULT_SAGE_AUTHORS) -> SageEvidence | None:
    """The most recent well-formed Sage review, marked current only if it reviewed `head_sha`."""
    for comment in reversed(comments):  # GitHub lists issue comments oldest first
        evidence = parse_sage_comment(comment, trusted)
        if evidence is not None:
            current = None if head_sha is None else evidence.reviewed_sha == head_sha
            return evidence.model_copy(update={"reviewed_head_current": current})
    return None
