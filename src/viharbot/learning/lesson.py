"""Archie's lesson draft: typed, versioned contracts (issue #143).

The draft is two separate types. `TraineeContent` is everything a learner may eventually see;
`InstructorContent` holds evaluation guidance and expected answers. They are validated, digested,
stored and published separately, so instructor-only text cannot ride along by accident: nothing in
`TraineeContent` has a field for it, and the publication path only ever receives `TraineeContent`.

Every substantive statement names its basis and the evidence ids behind it. A statement that is not
`unsupported` must cite at least one evidence id; `validate_provenance` checks the ids exist in the
collected bundle and that the basis matches the item, so a draft (including a model's) cannot upgrade
an author's claim to a verified one. A model-written draft is a draft, never evidence.

The 13 sections requested for the epic map onto fields as follows: title and topic; audience and
prerequisites; objectives; problem_context; architecture; alternatives; worked_examples; exercises
and checks; security_operational_governance; (instructor evaluation guidance lives in
`InstructorContent`); source_references with evidence_confidence; accessibility; limitations and
open_questions.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ..curriculum.models import Exercise, LessonId
from .basis import Basis
from .models import Repo, Sha

SCHEMA_VERSION = "1.0"
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
EvidenceId = Annotated[str, StringConstraints(pattern=r"^E-[0-9a-f]{10}$")]
CheckId = Annotated[str, StringConstraints(pattern=r"^c[0-9]{1,2}$")]
# Only public GitHub pull-request or commit links; never the instructor repository.
_SAFE_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/([A-Za-z0-9_.-]+)/(?:pull/[0-9]+|commit/[0-9a-f]{40})$")


def _safe_url(value: str) -> str:
    m = _SAFE_URL.match(value)
    if not m or "instructor" in m.group(1).lower():
        raise ValueError("source links must be GitHub pull-request or commit URLs outside the instructor repository")
    return value


SafeUrl = Annotated[str, AfterValidator(_safe_url)]
MEASURABLE_VERBS = frozenset(
    "describe explain identify compare evaluate apply analyze analyse justify trace list distinguish design "
    "critique summarize summarise classify predict demonstrate cite state outline assess".split())


class _F(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Statement(_F):
    text: Text
    basis: Basis
    refs: tuple[EvidenceId, ...] = ()

    @model_validator(mode="after")
    def _cited(self) -> Self:
        if self.basis is Basis.INSTRUCTOR_GUIDANCE:
            raise ValueError("trainee content cannot rest on instructor guidance")
        if self.basis is not Basis.UNSUPPORTED and not self.refs:
            raise ValueError("a supported statement must cite evidence")
        return self


class Objective(_F):
    text: Short

    @model_validator(mode="after")
    def _measurable(self) -> Self:
        if self.text.split()[0].lower().strip(",:") not in MEASURABLE_VERBS:
            raise ValueError("an objective must start with a measurable verb")
        return self


class Alternative(_F):
    option: Short
    trade_off: Text
    refs: tuple[EvidenceId, ...] = ()


class WorkedExample(_F):
    title: Short
    walkthrough: Text
    refs: tuple[EvidenceId, ...] = ()


class Check(_F):
    """A comprehension prompt. The expected answer is instructor-only and lives elsewhere."""

    check_id: CheckId
    kind: Literal["recall", "explain", "apply", "reflect"]
    prompt: Short
    hint: Short | None = None


class SourceRef(_F):
    label: Short
    url: SafeUrl
    sha: Sha | None = None


class TraineeContent(_F):
    title: Short
    topic: Short
    audience: Short
    prerequisites: tuple[Short, ...] = ()
    objectives: tuple[Objective, ...] = Field(min_length=1, max_length=8)
    problem_context: tuple[Statement, ...] = Field(min_length=1)
    architecture: tuple[Statement, ...] = Field(min_length=1)
    alternatives: tuple[Alternative, ...] = ()
    worked_examples: tuple[WorkedExample, ...] = ()
    exercises: tuple[Exercise, ...] = ()
    checks: tuple[Check, ...] = ()
    security_operational_governance: tuple[Statement, ...] = ()
    source_references: tuple[SourceRef, ...] = Field(min_length=1)
    evidence_confidence: Confidence
    confidence_rationale: Short
    accessibility: tuple[Short, ...] = Field(min_length=1)
    limitations: tuple[Short, ...] = ()
    open_questions: tuple[Short, ...] = ()

    @model_validator(mode="after")
    def _unique_checks(self) -> Self:
        ids = [c.check_id for c in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("check ids must be unique")
        return self

    def statements(self) -> tuple[Statement, ...]:
        return self.problem_context + self.architecture + self.security_operational_governance

    def all_refs(self) -> set[str]:
        refs = {r for s in self.statements() for r in s.refs}
        refs |= {r for a in self.alternatives for r in a.refs} | {r for w in self.worked_examples for r in w.refs}
        return refs

    def texts(self) -> list[str]:
        """Every string a learner could read, for confidentiality scanning."""
        return [t for t in _walk(self.model_dump(mode="json")) if t]


class ExpectedAnswer(_F):
    check_id: CheckId
    answer: Text


class InstructorReference(_F):
    """Points at instructor material by hash only; the text is never copied here."""

    path_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12}$")]
    blob_sha: Short


class InstructorContent(_F):
    evaluation_guidance: tuple[Text, ...] = Field(min_length=1)
    expected_answers: tuple[ExpectedAnswer, ...] = ()
    instructor_references: tuple[InstructorReference, ...] = ()
    manual_assessment_required: bool = True  # designated no-AI assessments stay manual


class LessonDraft(_F):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    lesson_id: LessonId
    version: int = Field(ge=1)
    assignment_id: str
    workflow_id: str
    source_repository: Repo
    pull_request: int = Field(ge=1)
    source_sha: Sha
    artifact_version: Short
    review_refs: tuple[Short, ...] = ()  # e.g. the Sage comment URL the draft relied on
    origin: Literal["deterministic", "model_draft"]
    trainee: TraineeContent
    instructor: InstructorContent

    @model_validator(mode="after")
    def _answers_match_checks(self) -> Self:
        ids = {c.check_id for c in self.trainee.checks}
        if any(a.check_id not in ids for a in self.instructor.expected_answers):
            raise ValueError("an expected answer names a check that does not exist")
        return self


def _walk(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _walk(v)]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _walk(v)]
    return []


def _canonical(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def digest_of(model: BaseModel) -> str:
    return hashlib.sha256(_canonical(model).encode()).hexdigest()
