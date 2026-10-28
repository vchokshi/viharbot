"""Deterministic checks on a fact-check result. Pure: no model calls, no network.

The reviewer may propose a verbatim quote for each verdict. The application, not the model,
decides whether the quote really appears in the evidence that was supplied. A verdict that
needs a citation and has none that checks out is recorded as insufficient evidence instead.
"""

from __future__ import annotations

import re

from .schemas import ClaimReview, FactCheckReview, ResearchFindings

# Verdicts that assert something about the evidence and therefore must be backed by a quote.
NEEDS_CITATION = {
    "supported_by_supplied_evidence",
    "contradicted_by_supplied_evidence",
    "mixed_evidence",
}
DOWNGRADE_NOTE = " Downgraded: no quote from the supplied evidence could be confirmed for this verdict."
MISSING_NOTE = "The reviewer returned no review for this claim."
MISSING_FOLLOW_UP = "Review this claim against its evidence."
MAX_QUOTE = 400


def normalize(text: str) -> str:
    """Case, whitespace, quote-style and edge-punctuation insensitive form for comparison."""
    text = (text or "").translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'}))
    return re.sub(r"\s+", " ", text).strip().strip("\"'.…").strip().casefold()


def quote_in(quote: str | None, evidence: str) -> bool:
    cleaned = normalize(quote or "")
    # A very short quote would match almost anything, so it proves nothing.
    return len(cleaned) >= 12 and cleaned in normalize(evidence)


def _match(review: ClaimReview, position: int, supplied: ResearchFindings,
           used: set[int], same_count: bool) -> int | None:
    """Which supplied claim a review is about: by text, else by position when counts agree."""
    wanted = normalize(review.claim)
    for i, finding in enumerate(supplied.findings):
        if i not in used and normalize(finding.claim) == wanted:
            return i
    if same_count and position not in used:
        return position
    return None


def guard_fact_check(result: FactCheckReview, supplied: ResearchFindings | None) -> FactCheckReview:
    """Verify citations, downgrade unbacked verdicts, and make sure every claim is covered."""
    if supplied is None:
        return result
    used: set[int] = set()
    same_count = len(result.claims) == len(supplied.findings)
    checked: list[tuple[int | None, ClaimReview]] = []
    for position, review in enumerate(result.claims):
        at = _match(review, position, supplied, used, same_count)
        if at is not None:
            used.add(at)
        checked.append((at, _check(review, supplied, at)))
    # Reviews of supplied claims come first, in the order they were supplied.
    claims = [review for _, review in sorted(checked, key=lambda p: (p[0] is None, p[0] or 0))]
    for i, finding in enumerate(supplied.findings):
        if i not in used:
            claims.append(ClaimReview(
                claim=finding.claim, status="insufficient_evidence", rationale=MISSING_NOTE,
                follow_up=MISSING_FOLLOW_UP, citation_verified=None))
    return result.model_copy(update={"claims": claims})


def _check(review: ClaimReview, supplied: ResearchFindings, at: int | None) -> ClaimReview:
    quote = (review.evidence_quote or "").strip()[:MAX_QUOTE] or None
    pool = supplied.findings[at].evidence if at is not None else " ".join(f.evidence for f in supplied.findings)
    verified = quote_in(quote, pool) if quote else None
    update: dict = {"evidence_quote": quote, "citation_verified": verified}
    if review.status in NEEDS_CITATION and not verified:
        update.update(status="insufficient_evidence", rationale=review.rationale.rstrip() + DOWNGRADE_NOTE)
    return review.model_copy(update=update)
