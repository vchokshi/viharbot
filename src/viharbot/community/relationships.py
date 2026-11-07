"""Relationship records: collaboration history, never authorization (Epic #157, Phase C3).

A relationship says how two agents have worked together: the pairing is configured by the
personas' `collaboration` entries, and the record adds **counts of what happened** in an
engagement (reviews completed, issues found, findings, material findings). It is history, not
standing.

What a record deliberately cannot be:

* **A trust level or an access right.** There is no score, rank, seniority, weight, permission,
  approval or tool field, and every model forbids unknown fields. Nothing in this module is read
  by any authorization, delegation, binding or budget decision, and no runtime module imports it
  (tests check this structurally). Personality, perceived seniority and past outcomes can never
  bypass authorization.
* **An opinion.** Only integer counts derived from validated review exchanges; no free text.
* **Durable.** The ledger lives in memory for the caller's lifetime. Durable storage, retention,
  correction and deletion are Phase D and ADR-009, and are not built here.

Only the pairs the personas declare are recorded. A review between two agents whose personas do
not declare that relationship is refused rather than invented.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..identity_registry import default_registry
from ..personas.models import CollaborationPattern
from ..personas.registry import PersonaRegistry, default_persona_registry
from .models import ReviewExchange, ReviewOutcome
from .review_policy import MATERIAL_SEVERITIES

RELATIONSHIP_SCHEMA_VERSION = "1.0"
MAX_RELATIONSHIPS = 256
_COUNT = Annotated[int, Field(ge=0, le=1_000_000)]
_REF = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]


class RelationshipError(ValueError):
    """A relationship could not be recorded. Messages are fixed and contain no content."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OutcomeCounts(_Strict):
    """What happened, as counts. Nothing here is a judgment of the other agent."""

    reviews_completed: _COUNT = 0
    no_issues_found: _COUNT = 0
    issues_found: _COUNT = 0
    unable_to_review: _COUNT = 0
    findings: _COUNT = 0  # every finding the reviewer returned, including any a report omitted
    material_findings: _COUNT = 0  # high or critical, including any a report omitted

    @model_validator(mode="after")
    def _consistent(self) -> "OutcomeCounts":
        if self.reviews_completed != self.no_issues_found + self.issues_found + self.unable_to_review:
            raise ValueError("review outcomes must add up to reviews completed")
        if self.material_findings > self.findings:
            raise ValueError("material findings cannot exceed findings")
        return self

    def __add__(self, other: "OutcomeCounts") -> "OutcomeCounts":
        return OutcomeCounts(**{
            name: getattr(self, name) + getattr(other, name) for name in type(self).model_fields})


class RelationshipRecord(_Strict):
    """Outcome counts for one configured pairing within one engagement."""

    schema_version: Literal["1.0"] = RELATIONSHIP_SCHEMA_VERSION
    engagement_id: _REF
    from_agent: _REF
    to_agent: _REF
    pattern: CollaborationPattern
    outcomes: OutcomeCounts

    @model_validator(mode="after")
    def _agents_are_registered_and_distinct(self) -> "RelationshipRecord":
        registry = default_registry()
        if self.from_agent not in registry or self.to_agent not in registry:
            raise ValueError("agents must be registered identities")
        if self.from_agent == self.to_agent:
            raise ValueError("a relationship needs two different agents")
        return self


def counts_for_exchange(exchange: ReviewExchange) -> OutcomeCounts:
    """The counts one validated review exchange contributes."""
    report = exchange.report
    carried_material = sum(1 for f in report.findings if f.severity in MATERIAL_SEVERITIES)
    return OutcomeCounts(
        reviews_completed=1,
        no_issues_found=int(report.outcome is ReviewOutcome.NO_ISSUES_FOUND),
        issues_found=int(report.outcome is ReviewOutcome.ISSUES_FOUND),
        unable_to_review=int(report.outcome is ReviewOutcome.UNABLE_TO_REVIEW),
        findings=len(report.findings) + report.omitted_findings,
        material_findings=carried_material + report.omitted_material_findings,
    )


class RelationshipLedger:
    """In-memory collaboration history, keyed by engagement and configured pairing."""

    def __init__(self, personas: PersonaRegistry | None = None) -> None:
        self._personas = personas or default_persona_registry()
        self._counts: dict[tuple[str, str, str, CollaborationPattern], OutcomeCounts] = {}

    def is_configured(self, from_agent: str, to_agent: str, pattern: CollaborationPattern) -> bool:
        """True if the persona of `from_agent` declares this pairing and pattern."""
        if from_agent not in self._personas:
            return False
        return any(
            entry.agent_id == to_agent and entry.pattern is pattern
            for entry in self._personas.get(from_agent).collaboration)

    def record_exchange(self, exchange: ReviewExchange) -> RelationshipRecord:
        """Add one validated review to the reviewer's history with the author.

        Raises `RelationshipError` if the personas do not declare that relationship or the
        ledger is full. Never raises for the content of the review.
        """
        reviewer, author = exchange.report.sender, exchange.request.author
        pattern = CollaborationPattern.REVIEWS
        if not self.is_configured(reviewer, author, pattern):
            raise RelationshipError("this relationship is not configured by the personas")
        key = (exchange.request.engagement_id, reviewer, author, pattern)
        if key not in self._counts and len(self._counts) >= MAX_RELATIONSHIPS:
            raise RelationshipError("the relationship ledger is full")
        self._counts[key] = self._counts.get(key, OutcomeCounts()) + counts_for_exchange(exchange)
        return self._record(key)

    def _record(self, key: tuple[str, str, str, CollaborationPattern]) -> RelationshipRecord:
        engagement, from_agent, to_agent, pattern = key
        return RelationshipRecord(
            engagement_id=engagement, from_agent=from_agent, to_agent=to_agent,
            pattern=pattern, outcomes=self._counts[key])

    def records(self) -> tuple[RelationshipRecord, ...]:
        """Every record, in a stable order (engagement, from, to, pattern)."""
        return tuple(self._record(key) for key in sorted(self._counts, key=lambda k: (*k[:3], k[3].value)))

    def __len__(self) -> int:
        return len(self._counts)
