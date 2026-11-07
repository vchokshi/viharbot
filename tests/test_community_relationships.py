"""Epic #157 Phase C3: relationship records are collaboration history, never authorization.

Offline. Exchanges are built from the repository's scripted fakes; no model is called.
"""

from __future__ import annotations

import inspect
import re
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestration_helpers import C, make_step
from test_review_policy import claim, edges, fact_step, request_for, research_state  # noqa: F401
from viharbot.community import ReviewExchange, review_policy
from viharbot.community import relationships as rel
from viharbot.community.relationships import (
    MAX_RELATIONSHIPS,
    OutcomeCounts,
    RelationshipError,
    RelationshipLedger,
    RelationshipRecord,
    counts_for_exchange,
)
from viharbot.community.review_policy import derive_report, record_review
from viharbot.personas import load_persona_registry
from viharbot.personas.models import CollaborationPattern
from viharbot.personas.registry import PERSONAS_DIR

SRC = Path(__file__).resolve().parent.parent / "src" / "viharbot"


def exchange_for(edges, claims, review=C.FACT_CHECK, engagement=None):
    state = research_state()
    request = request_for(edges, state, review)
    # The reviewer's own validated result: Scout's fact check, or Cipher's security assessment.
    step = (fact_step(claims) if review is C.FACT_CHECK
            else make_step(review, based_on={"research": 1}))
    report = derive_report(request, step)
    exchange = ReviewExchange(request=request, report=report)
    if engagement:  # same exchange, another engagement
        exchange = ReviewExchange(
            request=request.model_copy(update={"engagement_id": engagement}),
            report=report.model_copy(update={"engagement_id": engagement}))
    return exchange


ISSUES = [claim("contradicted_by_supplied_evidence", "bad"), claim("mixed_evidence", "mix")]
CLEAN = [claim("supported_by_supplied_evidence", "ok")]


# --- Counts ----------------------------------------------------------------------------------


def test_counts_for_an_exchange_with_issues(edges):
    counts = counts_for_exchange(exchange_for(edges, ISSUES))
    assert counts == OutcomeCounts(reviews_completed=1, issues_found=1, findings=2,
                                   material_findings=1)


def test_counts_for_a_clean_review(edges):
    counts = counts_for_exchange(exchange_for(edges, CLEAN))
    assert counts == OutcomeCounts(reviews_completed=1, no_issues_found=1)


def test_omitted_findings_are_included_in_the_counts(edges):
    claims = [claim("contradicted_by_supplied_evidence", f"bad-{n}") for n in range(23)]
    counts = counts_for_exchange(exchange_for(edges, claims))
    assert counts.findings == 23 and counts.material_findings == 23


def test_counts_must_be_consistent_and_non_negative():
    with pytest.raises(ValidationError, match="add up to reviews completed"):
        OutcomeCounts(reviews_completed=2, issues_found=1)
    with pytest.raises(ValidationError, match="cannot exceed findings"):
        OutcomeCounts(material_findings=1)
    with pytest.raises(ValidationError):
        OutcomeCounts(findings=-1)
    with pytest.raises(ValidationError):
        OutcomeCounts(findings=1_000_001)


def test_counts_add():
    a = OutcomeCounts(reviews_completed=1, issues_found=1, findings=2, material_findings=1)
    b = OutcomeCounts(reviews_completed=1, no_issues_found=1)
    assert a + b == OutcomeCounts(reviews_completed=2, issues_found=1, no_issues_found=1,
                                  findings=2, material_findings=1)


# --- The ledger ------------------------------------------------------------------------------


def test_the_ledger_accumulates_history_for_a_configured_pairing(edges):
    ledger = RelationshipLedger()
    ledger.record_exchange(exchange_for(edges, ISSUES))
    record = ledger.record_exchange(exchange_for(edges, CLEAN))
    assert (record.from_agent, record.to_agent, record.pattern) == (
        "scout", "nova", CollaborationPattern.REVIEWS)
    assert record.engagement_id == "unscoped"  # the placeholder engagement until real scoping
    assert record.outcomes == OutcomeCounts(
        reviews_completed=2, issues_found=1, no_issues_found=1, findings=2, material_findings=1)
    assert record.schema_version == "1.0"
    assert len(ledger) == 1


def test_history_is_kept_per_engagement_and_per_pairing(edges):
    ledger = RelationshipLedger()
    ledger.record_exchange(exchange_for(edges, ISSUES))
    ledger.record_exchange(exchange_for(edges, ISSUES, engagement="eng-2"))
    ledger.record_exchange(exchange_for(edges, ISSUES, review=C.SECURITY_ASSESSMENT))
    records = ledger.records()
    assert [(r.engagement_id, r.from_agent, r.to_agent) for r in records] == [
        ("eng-2", "scout", "nova"), ("unscoped", "cipher", "nova"), ("unscoped", "scout", "nova")]
    assert all(r.outcomes.reviews_completed == 1 for r in records)


def test_records_come_back_in_a_stable_order(edges):
    first, second = RelationshipLedger(), RelationshipLedger()
    exchanges = [exchange_for(edges, ISSUES, engagement="eng-b"),
                 exchange_for(edges, ISSUES, engagement="eng-a")]
    for exchange in exchanges:
        first.record_exchange(exchange)
    for exchange in reversed(exchanges):
        second.record_exchange(exchange)
    assert first.records() == second.records()


def test_only_relationships_the_personas_declare_are_recorded(edges, tmp_path):
    target = tmp_path / "definitions"
    shutil.copytree(PERSONAS_DIR, target)
    path = target / "scout.toml"
    text = path.read_text(encoding="utf-8")
    old = 'agent_id = "nova"\npattern = "reviews"'
    assert old in text
    path.write_text(text.replace(old, 'agent_id = "nova"\npattern = "consults"', 1), encoding="utf-8")
    ledger = RelationshipLedger(load_persona_registry(target))
    with pytest.raises(RelationshipError, match="not configured by the personas"):
        ledger.record_exchange(exchange_for(edges, ISSUES))  # Scout reviewing Nova
    assert len(ledger) == 0
    # Cipher's pairing with Nova is still declared, so it is still recorded.
    assert ledger.record_exchange(exchange_for(edges, ISSUES, review=C.SECURITY_ASSESSMENT))


def test_is_configured_reads_the_personas():
    ledger = RelationshipLedger()
    assert ledger.is_configured("scout", "nova", CollaborationPattern.REVIEWS)
    assert ledger.is_configured("cipher", "nova", CollaborationPattern.REVIEWS)
    assert not ledger.is_configured("nova", "scout", CollaborationPattern.REVIEWS)
    assert not ledger.is_configured("scout", "nova", CollaborationPattern.DEFERS_TO)
    assert not ledger.is_configured("ghost", "nova", CollaborationPattern.REVIEWS)


def test_the_ledger_is_bounded(edges, monkeypatch):
    monkeypatch.setattr(rel, "MAX_RELATIONSHIPS", 1)
    ledger = RelationshipLedger()
    ledger.record_exchange(exchange_for(edges, ISSUES))
    ledger.record_exchange(exchange_for(edges, CLEAN))  # same key: still fine
    with pytest.raises(RelationshipError, match="ledger is full"):
        ledger.record_exchange(exchange_for(edges, ISSUES, engagement="eng-2"))
    assert MAX_RELATIONSHIPS == 256


def test_recording_through_the_review_helper_matches(edges):
    state = research_state()
    request = request_for(edges, state)
    exchange = record_review(request, fact_step(ISSUES))
    assert RelationshipLedger().record_exchange(exchange).outcomes.issues_found == 1


# --- A record cannot be a trust level or an access right ------------------------------------------

FORBIDDEN = ("trust", "score", "rank", "senior", "weight", "access", "permission", "scope",
             "approv", "authori", "tool", "credential", "grant", "privilege", "reputation")


def field_names(model, seen=None):
    seen = seen if seen is not None else set()
    for name, info in model.model_fields.items():
        seen.add(name)
        annotation = info.annotation
        if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
            field_names(annotation, seen)
    return seen


def test_no_field_can_express_trust_rank_or_access():
    names = field_names(RelationshipRecord)
    assert names == {"schema_version", "engagement_id", "from_agent", "to_agent", "pattern",
                     "outcomes", "reviews_completed", "no_issues_found", "issues_found",
                     "unable_to_review", "findings", "material_findings"}
    assert not [n for n in names for word in FORBIDDEN if word in n.lower()]


@pytest.mark.parametrize("extra", [
    {"trust_score": 0.99}, {"seniority": "senior"}, {"rank": 1}, {"weight": 0.5},
    {"permissions": ["deploy"]}, {"scopes": ["admin"]}, {"approved": True},
    {"tools": ["shell"]}, {"model_alias": "x/y"}, {"access_level": 9}, {"notes": "free text"},
])
def test_unknown_fields_are_rejected_on_records_and_counts(extra):
    base = {"engagement_id": "e", "from_agent": "scout", "to_agent": "nova",
            "pattern": "reviews", "outcomes": {}}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RelationshipRecord.model_validate({**base, **extra})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RelationshipRecord.model_validate({**base, "outcomes": {**extra}})


@pytest.mark.parametrize(("update", "match"), [
    ({"from_agent": "ghost"}, "registered identities"),
    ({"to_agent": "scout"}, "two different agents"),
    ({"schema_version": "2.0"}, "schema_version"),
    ({"pattern": "worships"}, "pattern"),
    ({"engagement_id": "has space"}, "engagement_id"),
])
def test_records_are_validated(update, match):
    base = {"engagement_id": "e", "from_agent": "scout", "to_agent": "nova",
            "pattern": "reviews", "outcomes": {}}
    with pytest.raises(ValidationError, match=match):
        RelationshipRecord.model_validate({**base, **update})


def test_records_are_immutable():
    record = RelationshipRecord(engagement_id="e", from_agent="scout", to_agent="nova",
                                pattern=CollaborationPattern.REVIEWS, outcomes=OutcomeCounts())
    with pytest.raises(ValidationError):
        record.outcomes = OutcomeCounts(findings=1)  # type: ignore[misc]


# --- No authorization path can read a relationship ----------------------------------------------

AUTHORIZATION_MODULES = (
    "orchestration_policy.py", "execution_bindings.py", "identity.py", "identity_registry.py",
    "identity_execution.py", "delegation.py", "delegation_planner.py", "delegated_execution.py",
    "execution_planner.py", "orchestration_planner.py", "service_auth.py", "outbound_auth.py",
    "deadline.py", "inference_config.py", "skills/registry.py",
)


def test_no_runtime_module_imports_the_relationship_ledger():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "relationships.py":
            continue
        if re.search(r"^\s*(from|import)\s+[.\w]*relationships\b", path.read_text(encoding="utf-8"), re.M):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


def test_authorization_and_delegation_modules_never_mention_relationships():
    for name in AUTHORIZATION_MODULES:
        path = SRC / name
        assert path.exists(), name
        assert "relationship" not in path.read_text(encoding="utf-8").lower(), name


def test_the_review_policy_and_loop_do_not_use_relationship_history():
    for module in ("community/review_policy.py", "iterative_execution.py", "orchestration_state.py"):
        assert "relationship" not in (SRC / module).read_text(encoding="utf-8").lower(), module


def test_authorization_functions_cannot_be_given_a_relationship():
    from viharbot.orchestration_policy import allowed_capabilities, authorize_action

    for function in (authorize_action, allowed_capabilities, review_policy.authorize_review,
                     review_policy.plan_review, review_policy.build_review_request):
        for name, parameter in inspect.signature(function).parameters.items():
            text = f"{name} {parameter.annotation}".lower()
            assert "relationship" not in text and "ledger" not in text, (function.__name__, name)


def test_the_package_init_does_not_load_the_ledger():
    init = (SRC / "community" / "__init__.py").read_text(encoding="utf-8")
    assert "relationships" not in init and "RelationshipLedger" not in init


def test_the_ledger_is_in_memory_only():
    source = (SRC / "community" / "relationships.py").read_text(encoding="utf-8")
    for word in ("open(", "write_text", "sqlite", "pickle", "shelve", "jsonl", "requests", "urllib",
                 "socket", "boto3"):
        assert word not in source, word
