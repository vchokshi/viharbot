"""Community interaction contracts (Epic #157, Phase C1).

Typed, validated messages for peer review and constructive disagreement. They implement the
structure required by Agent Constitution sections 5.3 to 5.6: sender, recipient, engagement,
task, message type and correlation ids; the five parts of a challenge; disclosure of unresolved
disagreement.

These are data contracts only. Nothing here executes, authorizes, schedules or sends anything,
and no runtime path reads them yet (C2 will have the orchestrator use them). A message grants
no authority: every model forbids unknown fields, so it cannot carry tools, permissions, scopes,
model aliases, budgets or approvals. Execution budgets are set by application policy, never by a
message. Message contents are untrusted data to whoever receives them, never instructions.

An evidence reference is a structural citation of material that was supplied in the engagement.
Requiring one makes an unsupported challenge unrepresentable; it does not prove the claim true.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from ..identity_registry import default_registry

# The application may send review requests on a policy-authorized edge. It is not an agent
# identity and cannot disagree. (Equal to live_events.APPLICATION_IDENTITY; tested.)
APPLICATION_SENDER = "application"
MAX_MESSAGE_BYTES = 32 * 1024

Ref = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]
RequestId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Excerpt = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ContractError(ValueError):
    """A message was invalid, or a reply did not match the message it answers."""


class MessageType(StrEnum):
    REVIEW_REQUEST = "review_request"
    REVIEW_REPORT = "review_report"
    CHALLENGE = "challenge"
    CHALLENGE_RESOLUTION = "challenge_resolution"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReviewOutcome(StrEnum):
    NO_ISSUES_FOUND = "no_issues_found"
    ISSUES_FOUND = "issues_found"
    UNABLE_TO_REVIEW = "unable_to_review"


class ResolutionOutcome(StrEnum):
    ACCEPTED = "accepted"
    PARTLY_ACCEPTED = "partly_accepted"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _registered(agent_id: str) -> str:
    if agent_id not in default_registry():
        raise ValueError("agent is not a registered identity")
    return agent_id


def _unique(values: tuple[object, ...], what: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{what} must not repeat")


class EvidenceRef(_Strict):
    """A citation of material supplied in the engagement. Structural only."""

    artifact_ref: Ref
    locator: Line | None = None  # e.g. a section, line range or finding id inside the artifact
    excerpt: Excerpt | None = None  # a short quoted passage; never a place to carry instructions


Evidence = Annotated[tuple[EvidenceRef, ...], Field(min_length=1, max_length=10)]


class _Message(_Strict):
    """Envelope fields every message carries (Constitution 5.4)."""

    message_id: Ref
    engagement_id: Ref
    task_id: Ref  # the parent task this exchange belongs to
    request_id: RequestId  # correlates with the existing per-request id and structured logs
    sender: Ref
    recipient: Ref
    in_reply_to: Ref | None = None

    @field_validator("sender")
    @classmethod
    def _sender_known(cls, value: str) -> str:
        return value if value == APPLICATION_SENDER else _registered(value)

    @field_validator("recipient")
    @classmethod
    def _recipient_known(cls, value: str) -> str:
        return value if value == APPLICATION_SENDER else _registered(value)

    # The application sends review requests and receives their reports (the return route for a
    # request it originated). It sends and receives nothing else, and it never disagrees.
    application_may_send: ClassVar[bool] = False
    application_may_receive: ClassVar[bool] = False

    @model_validator(mode="after")
    def _envelope_rules(self) -> "_Message":
        if self.sender == self.recipient:
            raise ValueError("sender and recipient must differ")
        if self.in_reply_to == self.message_id:
            raise ValueError("a message cannot reply to itself")
        if self.sender == APPLICATION_SENDER and not self.application_may_send:
            raise ValueError("the application cannot send this message type")
        if self.recipient == APPLICATION_SENDER and not self.application_may_receive:
            raise ValueError("the application cannot receive this message type")
        return self


class ReviewRequest(_Message):
    """Ask `recipient` to independently review work by `author` (delegation contract, 5.3)."""

    application_may_send: ClassVar[bool] = True
    message_type: Literal["review_request"] = "review_request"
    author: Ref  # whose work is reviewed; the reviewer must be someone else
    artifact_refs: Annotated[tuple[Ref, ...], Field(min_length=1, max_length=10)]
    objective: Text
    scope: Text
    completion_criteria: Text
    escalation_conditions: Text
    focus: Annotated[tuple[Line, ...], Field(max_length=8)] = ()

    @field_validator("author")
    @classmethod
    def _author_known(cls, value: str) -> str:
        return _registered(value)

    @field_validator("artifact_refs")
    @classmethod
    def _refs_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, "artifact_refs")
        return value

    @model_validator(mode="after")
    def _reviewer_is_independent(self) -> "ReviewRequest":
        if self.recipient == self.author:
            raise ValueError("the reviewer must not be the author of the work")
        return self


class Finding(_Strict):
    """One review finding. It must cite evidence; an uncited finding cannot be expressed."""

    severity: Severity
    summary: Line
    detail: Text
    evidence: Evidence
    suggested_resolution: Text | None = None


class ReviewReport(_Message):
    application_may_receive: ClassVar[bool] = True
    message_type: Literal["review_report"] = "review_report"
    outcome: ReviewOutcome
    findings: Annotated[tuple[Finding, ...], Field(max_length=20)] = ()
    # What was and was not examined (Constitution 5.8: material limitations are documented).
    limitations: Annotated[tuple[Line, ...], Field(min_length=1, max_length=8)]
    reason_unable: Text | None = None

    @model_validator(mode="after")
    def _outcome_matches_content(self) -> "ReviewReport":
        if self.in_reply_to is None:
            raise ValueError("a review report must reply to a review request")
        if self.outcome is ReviewOutcome.ISSUES_FOUND and not self.findings:
            raise ValueError("issues_found requires at least one finding")
        if self.outcome is not ReviewOutcome.ISSUES_FOUND and self.findings:
            raise ValueError("findings are only allowed with issues_found")
        if self.outcome is ReviewOutcome.UNABLE_TO_REVIEW and not self.reason_unable:
            raise ValueError("unable_to_review requires a reason")
        if self.outcome is not ReviewOutcome.UNABLE_TO_REVIEW and self.reason_unable:
            raise ValueError("reason_unable is only allowed with unable_to_review")
        return self


class Challenge(_Message):
    """Constructive disagreement (Constitution 5.5): all five parts are mandatory."""

    message_type: Literal["challenge"] = "challenge"
    artifact_ref: Ref  # the work containing the disputed conclusion
    disputed_conclusion: Text
    reason: Text
    evidence: Evidence
    potential_consequence: Text
    proposed_resolution: Text


class ChallengeResolution(_Message):
    message_type: Literal["challenge_resolution"] = "challenge_resolution"
    outcome: ResolutionOutcome
    rationale: Text
    evidence: Annotated[tuple[EvidenceRef, ...], Field(max_length=10)] = ()
    # Constitution 5.6: material unresolved disagreement is disclosed or escalated.
    material: bool = False
    escalation: Text | None = None

    @model_validator(mode="after")
    def _resolution_rules(self) -> "ChallengeResolution":
        if self.in_reply_to is None:
            raise ValueError("a resolution must reply to a challenge")
        if self.outcome is not ResolutionOutcome.UNRESOLVED and not self.evidence:
            raise ValueError("a decided resolution must cite evidence")
        if self.outcome is ResolutionOutcome.UNRESOLVED and self.material and not self.escalation:
            raise ValueError("a material unresolved disagreement requires an escalation")
        if self.escalation and self.outcome is not ResolutionOutcome.UNRESOLVED:
            raise ValueError("escalation is only for an unresolved disagreement")
        return self


CommunityMessage = Annotated[
    Union[ReviewRequest, ReviewReport, Challenge, ChallengeResolution],
    Field(discriminator="message_type"),
]
_adapter: TypeAdapter[CommunityMessage] = TypeAdapter(CommunityMessage)


AnyMessage = ReviewRequest | ReviewReport | Challenge | ChallengeResolution


def _known_names() -> frozenset[str]:
    """Every field name and message type the contracts define. Anything else in an error
    location came from the sender and is replaced, because it may carry sensitive text."""
    names: set[str] = {member.value for member in MessageType}
    for model in (ReviewRequest, ReviewReport, Challenge, ChallengeResolution, Finding, EvidenceRef):
        names.update(model.model_fields)
    return frozenset(names)


_KNOWN_NAMES = _known_names()
# Fixed text per pydantic error type. Pydantic's own messages can quote the rejected input
# (for example an invalid discriminator tag), so only our own validators' text is passed through.
_GENERIC = {
    "missing": "field required",
    "extra_forbidden": "unknown field not permitted",
    "union_tag_invalid": "unknown message type",
    "union_tag_not_found": "message type missing",
    "json_invalid": "not valid JSON",
    "too_short": "too few items",
    "too_long": "too many items",
    "string_too_short": "text is empty or too short",
    "string_too_long": "text is too long",
    "string_pattern_mismatch": "value has an invalid format",
    "literal_error": "value is not allowed",
    "enum": "value is not allowed",
}


def _describe(error: ValidationError) -> str:
    """Field paths and fixed messages only. Never echoes rejected values or sender-chosen names."""
    parts = []
    for item in error.errors(include_input=False, include_url=False):
        where = ".".join(
            str(part) if isinstance(part, int) or part in _KNOWN_NAMES else "<unknown>"
            for part in item["loc"]
        ) or "document"
        if item["type"] == "value_error":  # raised by this module's validators: static text
            message = str(item["msg"]).removeprefix("Value error, ")
        else:
            message = _GENERIC.get(item["type"], f"invalid value ({item['type']})")
        parts.append(f"{where}: {message}")
    return "; ".join(parts)


def parse_message(payload: object) -> AnyMessage:
    """Validate a decoded JSON object. Raises `ContractError` without echoing rejected values."""
    try:
        return _adapter.validate_python(payload)
    except ValidationError as exc:
        raise ContractError(_describe(exc)) from None


def parse_message_json(raw: str | bytes) -> AnyMessage:
    """Parse a JSON document, rejecting oversized input before decoding it."""
    size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    if size > MAX_MESSAGE_BYTES:
        raise ContractError("message exceeds the size limit")
    try:
        return _adapter.validate_json(raw)
    except ValidationError as exc:
        raise ContractError(_describe(exc)) from None


def validate_reply(
    original: ReviewRequest | Challenge,
    reply: ReviewReport | ChallengeResolution,
) -> None:
    """Check that `reply` genuinely answers `original`. Raises `ContractError` otherwise.

    A reply must come from the original's recipient, go back to its sender, carry the same
    engagement, task and request id, and name the original's message id.
    """
    expected = {
        ReviewRequest: ReviewReport,
        Challenge: ChallengeResolution,
    }[type(original)]
    if not isinstance(reply, expected):
        raise ContractError("reply type does not match the original message")
    if reply.in_reply_to != original.message_id:
        raise ContractError("reply does not reference the original message")
    if (reply.engagement_id, reply.task_id, reply.request_id) != (
        original.engagement_id, original.task_id, original.request_id
    ):
        raise ContractError("reply is not in the same engagement, task and request")
    if reply.sender != original.recipient or reply.recipient != original.sender:
        raise ContractError("reply must come from the original recipient to its sender")
