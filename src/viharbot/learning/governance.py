"""Deterministic comparison of a pull request with ViharBot's written governance (issue #142).

No model and no network call: it compares retrieved evidence (changed paths, the description's
headings, Sage's review) with a few rules that can be checked mechanically. It is not a compliance
verdict. It cannot judge whether reasoning is good, and a pull request's own text is data that can
never relax a rule. Where evidence and a rule disagree, or the evidence cannot show a rule was met,
the note is a `conflict`: recorded with its source and left for a human.

Rules, and where each is written:
- Founder-review paths: `AGENTS.md` (Working agreements) and `.github/CODEOWNERS`.
- Pull request rationale: `docs/governance/change-rationale-policy.md` and
  `.github/pull_request_template.md`.
- Sage advice is advisory (`docs/education-agents.md`); an unaddressed HIGH or CRITICAL finding is
  worth teaching from, so it is surfaced, not judged.
"""

from __future__ import annotations

import re

from .description import sections
from .models import GovernanceNote, PRSnapshot

# AGENTS.md, "Working agreements": founder-reviewed merge is required for these paths. A test checks
# that this list still matches AGENTS.md, so a change there cannot silently outdate it.
FOUNDER_REVIEW_PATHS: tuple[str, ...] = (
    "AGENTS.md", "CONTRIBUTING.md", "CONTRIBUTORS.md", "CLA-ACCEPTANCES.md", "CLA-EXCEPTIONS.md",
    "docs/governance/", "docs/adr/", ".github/",
)
# The change-rationale policy asks a pull request to explain why it was made, the chosen approach,
# alternatives (or why none), trade-offs, validation and operational implications. Each entry is
# (label, pattern matched against a lower-cased description heading); the labels are the headings of
# `.github/pull_request_template.md`. "why" must not match "why this way".
RATIONALE_SECTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("WHY", re.compile(r"^(why\b(?!\s+this way)|motivation)")),
    ("WHY THIS WAY", re.compile(r"^why this way")),
    ("Alternatives considered", re.compile(r"^alternatives")),
    ("Trade-offs and risks", re.compile(r"^(trade-?\s?offs|risks)")),
    ("Validation and evidence", re.compile(r"^(validation|evidence|tests?\b)")),
    ("Deployment, rollback, and follow-up", re.compile(r"^(deployment|rollback)")),
)
_BLOCKING = ("critical", "high")


def is_founder_review_path(path: str) -> bool:
    """A listed file matches exactly; a listed directory (trailing slash) matches everything under it."""
    return any(path == p if not p.endswith("/") else path.startswith(p) for p in FOUNDER_REVIEW_PATHS)


def compare(snapshot: PRSnapshot) -> tuple[GovernanceNote, ...]:
    src = f"{snapshot.repository}#{snapshot.number}"
    notes: list[GovernanceNote] = []

    protected = [f.path for f in snapshot.files if is_founder_review_path(f.path)]
    if protected:
        listed = ", ".join(protected[:10]) + (f" and {len(protected) - 10} more" if len(protected) > 10 else "")
        notes.append(GovernanceNote(
            status="conflict",
            finding=(f"The pull request changed founder-review paths: {listed}. The retrieved evidence "
                     "does not show whether the founder reviewed the merge."),
            basis="verified_in_diff", policy="AGENTS.md, Working agreements; .github/CODEOWNERS",
            source=f"{src} changed files"))

    headings = list(sections(snapshot.body))
    missing = [label for label, pattern in RATIONALE_SECTIONS
               if not any(pattern.match(h) for h in headings)]
    if missing:
        notes.append(GovernanceNote(
            status="conflict" if len(missing) == len(RATIONALE_SECTIONS) else "observation",
            finding=("The description has none of the expected rationale sections."
                     if len(missing) == len(RATIONALE_SECTIONS)
                     else "The description has no section matching: " + ", ".join(missing) + "."),
            basis="author_statement" if headings else "not_available",
            policy="docs/governance/change-rationale-policy.md, Pull requests",
            source=f"{src} description"))
    else:
        notes.append(GovernanceNote(
            status="observation",
            finding="The description has a section for every expected rationale topic. Their quality "
                    "is for a human to judge.",
            basis="author_statement", policy="docs/governance/change-rationale-policy.md, Pull requests",
            source=f"{src} description"))

    review = snapshot.sage_review
    if review is None:
        notes.append(GovernanceNote(
            status="observation",
            finding="No Sage review by a trusted author was found, so there is no review evidence.",
            basis="not_available", policy="docs/education-agents.md, Sage review evidence",
            source=f"{src} comments"))
    else:
        blocking = sum(f.severity in _BLOCKING for f in review.findings)
        advised = review.recommendation == "REQUEST_CHANGES" or blocking > 0
        stale = review.reviewed_head_current is False
        if advised or stale:
            reasons = []
            if review.recommendation == "REQUEST_CHANGES":
                reasons.append("recommended REQUEST_CHANGES")
            if blocking:
                reasons.append(f"reported {blocking} HIGH or CRITICAL finding(s)")
            if stale:
                reasons.append("reviewed a commit that is not the pull request's final head")
            notes.append(GovernanceNote(
                status="conflict",
                finding=("Sage " + " and ".join(reasons) + ". The retrieved evidence does not show "
                         "whether the findings were addressed before merge."),
                basis="sage_advisory", policy="docs/education-agents.md, Sage review evidence",
                source=review.comment_url or f"{src} Sage comment"))
        else:
            notes.append(GovernanceNote(
                status="observation",
                finding=f"Sage's review of the final head recommended {review.recommendation} with no "
                        "HIGH or CRITICAL findings.",
                basis="sage_advisory", policy="docs/education-agents.md, Sage review evidence",
                source=review.comment_url or f"{src} Sage comment"))
    return tuple(notes)
