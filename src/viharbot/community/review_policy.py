"""Peer-review edges in the orchestration policy (Epic #157, Phase C2).

A review edge says that a delegated run of a *review capability* on a fresh result of an *artifact
capability* counts as an independent peer review of that result. This module adds no engine:

* An edge is only a label over delegations the existing orchestrator already runs. Authorizing a
  review calls the existing `authorize_action`, so bindings, dependencies, freshness, the repeat
  limit and the execution, decision and time budgets all apply unchanged. Cancellation and the
  request deadline are honored by the same loop that runs the delegation.
* Identities (author, reviewer) always come from trusted state and the approved bindings, never
  from the message. A message that names a different author or reviewer is rejected.
* The report is derived deterministically from the reviewer's already-validated structured
  result. No model call is made and nothing is generated, so a finding cannot be fabricated:
  each one maps to a field the reviewing service actually returned.

Everything here is pure: no network, no model, no agent invocation.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Collection
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from ..execution_bindings import ExecutionBindingSet, default_bindings, load_bindings
from ..identity import Capability, CapabilityStatus, capability_status
from ..identity_registry import AgentRegistry, default_registry
from ..orchestration_policy import (
    HARD_DEPENDENCIES,
    SOFT_INPUTS,
    ActionRejected,
    AuthorizedAction,
    OrchestrationAction,
    authorize_action,
    fresh_latest,
)
from ..orchestration_state import OrchestrationState, StepResult
from ..schemas import FactCheckReview, SecurityAssessment, SourceQualityReview
from .models import (
    APPLICATION_SENDER,
    EvidenceRef,
    Finding,
    ReviewExchange,
    ReviewOutcome,
    ReviewReport,
    ReviewRequest,
    Severity,
)

PEER_REVIEW_FLAG = "VIHARBOT_PEER_REVIEW_ENABLED"
EDGES_FILE = Path(__file__).resolve().parent.parent / "execution_config" / "review_edges.toml"
# The engagement placeholder already used for outbound authorization (outbound_auth.py); real
# engagement scoping does not exist yet.
ENGAGEMENT_ID = "unscoped"
MAX_REVIEWS_PER_REQUEST = 8
MAX_FINDINGS = 20
_ARTIFACT_REF = re.compile(r"^([a-z_]+):v([1-9][0-9]{0,3})$")


class ReviewConfigError(ValueError):
    """The review edge configuration was invalid."""


class ReviewRejected(Exception):
    """The application refused a review. `code` is fixed and content-free."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def peer_review_enabled() -> bool:
    """Off unless this process's own environment turns it on. Read per request."""
    return os.getenv(PEER_REVIEW_FLAG, "false").strip().lower() in {"1", "true", "yes"}


# --- Edges -----------------------------------------------------------------------------------


class ReviewEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_capability: Capability
    review_capability: Capability


# What each review capability's validated result can be turned into. Anything else cannot be an
# edge, because a report could not be derived from it without inventing content.
SUPPORTED_REVIEW_CAPABILITIES = frozenset(
    {Capability.FACT_CHECK, Capability.SOURCE_QUALITY, Capability.SECURITY_ASSESSMENT}
)


class ReviewEdgeSet:
    """Edges validated against the identity registry and the approved bindings."""

    def __init__(
        self, edges: tuple[ReviewEdge, ...], registry: AgentRegistry, bindings: ExecutionBindingSet
    ) -> None:
        seen: set[ReviewEdge] = set()
        for index, edge in enumerate(edges):
            where = f"edge[{index}]"
            if edge in seen:
                raise ReviewConfigError(f"{where}: duplicate edge")
            seen.add(edge)
            artifact, review = edge.artifact_capability, edge.review_capability
            if artifact not in HARD_DEPENDENCIES or review not in HARD_DEPENDENCIES:
                raise ReviewConfigError(f"{where}: capability is not an orchestrated capability")
            for capability in (artifact, review):
                if capability_status(capability) is not CapabilityStatus.IMPLEMENTED:
                    raise ReviewConfigError(f"{where}: capability is not implemented")
            if review not in SUPPORTED_REVIEW_CAPABILITIES:
                raise ReviewConfigError(f"{where}: no report can be derived for this capability")
            if artifact not in (*HARD_DEPENDENCIES[review], *SOFT_INPUTS.get(review, ())):
                raise ReviewConfigError(f"{where}: the reviewer does not consume the artifact")
            author = bindings.lookup(artifact)
            reviewer = bindings.lookup(review)
            if author is None or reviewer is None or not (author.enabled and reviewer.enabled):
                raise ReviewConfigError(f"{where}: both capabilities need an enabled binding")
            if author.identity_id not in registry or reviewer.identity_id not in registry:
                raise ReviewConfigError(f"{where}: identity is not registered")
            if author.identity_id == reviewer.identity_id:
                raise ReviewConfigError(f"{where}: the reviewer must differ from the author")
        self._edges = tuple(edges)

    @property
    def edges(self) -> tuple[ReviewEdge, ...]:
        return self._edges

    def for_review(self, review_capability: Capability) -> tuple[ReviewEdge, ...]:
        return tuple(e for e in self._edges if e.review_capability is review_capability)

    def find(self, artifact: Capability, review: Capability) -> ReviewEdge | None:
        return next(
            (e for e in self._edges
             if e.artifact_capability is artifact and e.review_capability is review), None)

    def __len__(self) -> int:
        return len(self._edges)


def load_review_edges(
    registry: AgentRegistry | None = None,
    bindings: ExecutionBindingSet | None = None,
    path: Path | None = None,
) -> ReviewEdgeSet:
    """Load edges from repository-controlled configuration. `path` is for tests only."""
    registry = registry or default_registry()
    if bindings is None:
        bindings = default_bindings() if registry is default_registry() else load_bindings(registry)
    source = path or EDGES_FILE
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ReviewConfigError(f"{source.name}: invalid TOML ({exc})") from None
    if set(data) - {"edge"}:
        raise ReviewConfigError(f"{source.name}: unexpected top-level keys")
    entries = data.get("edge", [])
    if not isinstance(entries, list) or not entries:
        raise ReviewConfigError(f"{source.name}: no edges defined")
    parsed = []
    for index, entry in enumerate(entries):
        try:
            parsed.append(ReviewEdge.model_validate(entry))
        except ValidationError as exc:
            fields = "; ".join(
                ".".join(str(p) for p in item["loc"]) or "document"
                for item in exc.errors(include_input=False, include_url=False))
            raise ReviewConfigError(f"{source.name}: edge[{index}] is invalid ({fields})") from None
    return ReviewEdgeSet(tuple(parsed), registry, bindings)


@cache
def default_review_edges() -> ReviewEdgeSet:
    return load_review_edges()


# --- Requests --------------------------------------------------------------------------------

# Application-written briefs. They contain no model or user text.
_BRIEFS: dict[Capability, tuple[str, str, str, tuple[str, ...]]] = {
    Capability.FACT_CHECK: (
        "Independently check the claims in the research findings against the supplied evidence.",
        "The claims and their cited evidence in the research findings only.",
        "Each claim is marked supported, contradicted, mixed or insufficient, with limitations.",
        ("claim accuracy", "citation support"),
    ),
    Capability.SOURCE_QUALITY: (
        "Independently assess the quality of the sources the research findings rely on.",
        "The sources listed in the research findings only.",
        "Each source is rated with its limits, and unknown quality is reported as unknown.",
        ("source reliability",),
    ),
    Capability.SECURITY_ASSESSMENT: (
        "Independently assess the security implications of the research findings.",
        "The research findings as supplied; no system is scanned or probed.",
        "Risks are stated with an overall rating, recommendations and uncertainties.",
        ("security implications",),
    ),
}
_ESCALATION = "Report any inability to complete the review to the orchestrator; do not guess."


def artifact_ref(step: StepResult) -> str:
    """A stable reference to one validated result: `<capability>:v<version>`."""
    return f"{step.capability.value}:v{step.version}"


def task_id_for(state: OrchestrationState) -> str:
    return f"decision-{state.decision_number}"


def build_review_request(
    edge: ReviewEdge, state: OrchestrationState, bindings: ExecutionBindingSet, execution_id: str
) -> ReviewRequest:
    """The application's own request for an edge, built from trusted state and bindings."""
    artifact = fresh_latest(state, edge.artifact_capability)
    if artifact is None:
        raise ReviewRejected("artifact_unavailable")
    reviewer = bindings.lookup(edge.review_capability)
    if reviewer is None:
        raise ReviewRejected("edge_not_declared")
    objective, scope, criteria, focus = _BRIEFS[edge.review_capability]
    return ReviewRequest(
        message_id=f"review-{execution_id}",
        engagement_id=ENGAGEMENT_ID,
        task_id=task_id_for(state),
        request_id=state.request_id,
        sender=APPLICATION_SENDER,
        recipient=reviewer.identity_id,
        author=artifact.identity_id,
        artifact_refs=(artifact_ref(artifact),),
        objective=objective,
        scope=scope,
        completion_criteria=criteria,
        escalation_conditions=_ESCALATION,
        focus=focus,
    )


def authorize_review(
    request: ReviewRequest,
    review_capability: Capability,
    state: OrchestrationState,
    edges: ReviewEdgeSet,
    registry: AgentRegistry,
    bindings: ExecutionBindingSet,
    available_services: Collection[str],
    *,
    seconds_left: float | None = None,
) -> AuthorizedAction:
    """Authorize a review. Raises `ReviewRejected` with a fixed code, or returns the action.

    `review_capability` is supplied by the application (it is what is being delegated); it is
    never read from the message. The author and reviewer are checked against trusted state.
    """
    if request.sender != APPLICATION_SENDER:
        raise ReviewRejected("sender_not_application")
    if (request.request_id, request.engagement_id, request.task_id) != (
        state.request_id, ENGAGEMENT_ID, task_id_for(state)
    ):
        raise ReviewRejected("request_mismatch")
    if len(request.artifact_refs) != 1:
        raise ReviewRejected("artifact_unavailable")
    match = _ARTIFACT_REF.fullmatch(request.artifact_refs[0])
    if match is None:
        raise ReviewRejected("artifact_unavailable")
    try:
        artifact_capability = Capability(match.group(1))
    except ValueError:
        raise ReviewRejected("artifact_unavailable") from None
    latest = state.latest(artifact_capability)
    if latest is None:
        raise ReviewRejected("artifact_unavailable")
    current = fresh_latest(state, artifact_capability)
    if current is None or current.version != int(match.group(2)):
        raise ReviewRejected("artifact_stale")
    if request.author != current.identity_id:
        raise ReviewRejected("author_mismatch")
    if edges.find(artifact_capability, review_capability) is None:
        raise ReviewRejected("edge_not_declared")
    reviewer = bindings.lookup(review_capability)
    if reviewer is None or reviewer.identity_id != request.recipient:
        raise ReviewRejected("reviewer_mismatch")
    if request.recipient == request.author:  # also enforced by the contract and the edge loader
        raise ReviewRejected("self_review")
    try:
        # The same authorization every delegation gets: binding, dependencies, freshness,
        # repeat limit, execution budget and time budget.
        return authorize_action(
            OrchestrationAction(
                action="delegate", capability=review_capability.value, rationale="peer review"),
            state, registry, bindings, available_services, seconds_left=seconds_left)
    except ActionRejected as exc:
        raise ReviewRejected(exc.code) from None


def plan_review(
    review_capability: Capability,
    state: OrchestrationState,
    edges: ReviewEdgeSet,
    registry: AgentRegistry,
    bindings: ExecutionBindingSet,
    available_services: Collection[str],
    execution_id: str,
    *,
    seconds_left: float | None = None,
) -> ReviewRequest | None:
    """The authorized request for this delegation, or None when it is not a recognized review.

    Never raises for a policy outcome; `ReviewRejected` is a reason not to record a review, not a
    reason to stop the delegation, which the existing policy has already authorized.
    """
    if len(state.reviews) >= MAX_REVIEWS_PER_REQUEST:
        return None
    for edge in edges.for_review(review_capability):
        try:
            request = build_review_request(edge, state, bindings, execution_id)
            authorize_review(request, review_capability, state, edges, registry, bindings,
                             available_services, seconds_left=seconds_left)
        except ReviewRejected:
            continue
        return request
    return None


# --- Reports ---------------------------------------------------------------------------------

_BASE_LIMITATION = (
    "Derived from the reviewer's validated structured result; the application did not "
    "independently verify it."
)


def _clip(text: object, limit: int) -> str:
    value = " ".join(str(text).split())
    if not value:
        return "(empty)"
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _limitations(items: list[str], omitted: int = 0) -> tuple[str, ...]:
    """The base limitation, then a note when findings were omitted, then the reviewer's own."""
    notes: tuple[str, ...] = ()
    if omitted:
        notes = (
            f"{omitted} lower-priority finding(s) were omitted at the {MAX_FINDINGS}-finding "
            "limit; findings are listed most severe first.",
        )
    room = 8 - 1 - len(notes)
    extra = tuple(_clip(item, 200) for item in items if str(item).strip())[:room]
    return (_BASE_LIMITATION, *notes, *extra)


# Most severe first. Sorting is stable, so findings of equal severity keep the reviewer's order.
_SEVERITY_ORDER = (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO)


def _fact_check_findings(domain: FactCheckReview, ref: str) -> list[Finding]:
    severity = {
        "contradicted_by_supplied_evidence": Severity.HIGH,
        "mixed_evidence": Severity.MEDIUM,
        "insufficient_evidence": Severity.LOW,
    }
    findings = []
    for claim in domain.claims:
        level = severity.get(claim.status)
        if level is None:  # supported_by_supplied_evidence: nothing to report
            continue
        # A quote is attached only when the application itself verified it against the evidence.
        quote = _clip(claim.evidence_quote, 500) if claim.evidence_quote and claim.citation_verified else None
        findings.append(Finding(
            severity=level,
            summary=_clip(f"{claim.status.replace('_', ' ')}: {claim.claim}", 200),
            detail=_clip(claim.rationale, 1000),
            evidence=(EvidenceRef(artifact_ref=ref, locator=_clip(f"claim: {claim.claim}", 200),
                                  excerpt=quote),),
            suggested_resolution=_clip(claim.follow_up, 1000) if claim.follow_up.strip() else None,
        ))
    return findings


def _source_findings(domain: SourceQualityReview, ref: str) -> list[Finding]:
    severity = {"weak": Severity.MEDIUM, "unknown": Severity.LOW}
    findings = []
    for source in domain.sources:
        level = severity.get(source.apparent_quality)
        if level is None:
            continue
        checks = "; ".join(source.checks_to_perform)
        findings.append(Finding(
            severity=level,
            summary=_clip(f"{source.apparent_quality} source: {source.source}", 200),
            detail=_clip(source.rationale, 1000),
            evidence=(EvidenceRef(artifact_ref=ref, locator=_clip(f"source: {source.source}", 200)),),
            suggested_resolution=_clip(checks, 1000) if checks.strip() else None,
        ))
    return findings


def _security_findings(domain: SecurityAssessment, ref: str) -> list[Finding]:
    level = {"low": Severity.LOW, "medium": Severity.MEDIUM, "high": Severity.HIGH}[domain.overall_risk]
    advice = "; ".join(domain.recommendations[:3])
    return [
        Finding(
            severity=level,
            summary=_clip(f"{domain.overall_risk} risk: {implication}", 200),
            detail=_clip(implication, 1000),
            evidence=(EvidenceRef(artifact_ref=ref, locator=f"implication {index}"),),
            suggested_resolution=_clip(advice, 1000) if advice.strip() else None,
        )
        for index, implication in enumerate(domain.implications, start=1)
        if str(implication).strip()
    ]


def derive_report(request: ReviewRequest, step: StepResult) -> ReviewReport:
    """Turn the reviewer's validated result into a `ReviewReport` that answers `request`.

    Pure and deterministic. Raises `ReviewRejected` if the result cannot be mapped or does not
    belong to the request's reviewer.
    """
    if step.identity_id != request.recipient:
        raise ReviewRejected("reviewer_mismatch")
    ref = request.artifact_refs[0]
    domain = step.domain
    if step.capability is Capability.FACT_CHECK and isinstance(domain, FactCheckReview):
        findings, limits = _fact_check_findings(domain, ref), list(domain.limitations)
    elif step.capability is Capability.SOURCE_QUALITY and isinstance(domain, SourceQualityReview):
        findings, limits = _source_findings(domain, ref), list(domain.limitations)
    elif step.capability is Capability.SECURITY_ASSESSMENT and isinstance(domain, SecurityAssessment):
        findings, limits = _security_findings(domain, ref), list(domain.uncertainties)
    else:
        raise ReviewRejected("no_report_mapper")
    # Prioritize before capping, so a serious finding is never the one that is dropped.
    findings.sort(key=lambda finding: _SEVERITY_ORDER.index(finding.severity))
    kept, dropped = findings[:MAX_FINDINGS], findings[MAX_FINDINGS:]
    findings = kept
    omitted_material = sum(1 for finding in dropped if finding.severity in MATERIAL_SEVERITIES)
    return ReviewReport(
        message_id=f"report-{request.message_id.removeprefix('review-')}",
        engagement_id=request.engagement_id,
        task_id=request.task_id,
        request_id=request.request_id,
        sender=request.recipient,
        recipient=request.sender,
        in_reply_to=request.message_id,
        outcome=ReviewOutcome.ISSUES_FOUND if findings else ReviewOutcome.NO_ISSUES_FOUND,
        findings=tuple(findings),
        limitations=_limitations(limits, len(dropped)),
        omitted_findings=len(dropped),
        omitted_material_findings=omitted_material,
    )


def record_review(request: ReviewRequest, step: StepResult) -> ReviewExchange:
    """Derive and validate the exchange for a completed review run."""
    return ReviewExchange(request=request, report=derive_report(request, step))


MATERIAL_SEVERITIES = frozenset({Severity.HIGH, Severity.CRITICAL})


def material_finding_count(exchanges: list[ReviewExchange]) -> int:
    """Findings serious enough that Constitution 5.6 expects them disclosed, not buried.

    Counts the findings a report carries plus any material ones it had to omit, so the total
    covers the reviewer's whole result.
    """
    return sum(
        sum(1 for finding in exchange.report.findings if finding.severity in MATERIAL_SEVERITIES)
        + exchange.report.omitted_material_findings
        for exchange in exchanges
    )
