from __future__ import annotations

import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from viharbot import delegation
from viharbot.delegation import (
    DELEGABLE_WORKFLOWS,
    DelegationDecision,
    DelegationRejected,
    validate_delegation,
)
from viharbot.execution_bindings import ExecutionBinding, ExecutionBindingSet, default_bindings
from viharbot.execution_planner import WORKFLOWS, PlanningError, plan_workflow
from viharbot.identity import Capability
from viharbot.identity_registry import default_registry

REGISTRY = default_registry()
ALL = {"research", "fact-check", "source-quality", "security", "privacy-review", "synthesis"}


def decision(workflow="full_review", confidence="high", rationale="Because."):
    return DelegationDecision(workflow_name=workflow, rationale=rationale, confidence=confidence)


def validate(d, bindings=None, available=ALL):
    return validate_delegation(d, REGISTRY, bindings or default_bindings(), available)


# 1. valid recommendations
@pytest.mark.parametrize("workflow", ["full_review", "research_security"])
@pytest.mark.parametrize("confidence", ["low", "medium", "high"])
def test_valid_recommendations_are_accepted(workflow, confidence):
    d = decision(workflow, confidence, "  A short reason.  ")
    assert d.workflow_name == workflow and d.confidence == confidence
    assert d.rationale == "A short reason."


# 2. invalid workflow names
@pytest.mark.parametrize("name", [
    "", "synthesis_only", "FULL_REVIEW", "full_review; research_security", "../full_review",
    "research", "fact_check", None, 7, ["full_review"],
])
def test_invalid_workflow_names_are_rejected(name):
    with pytest.raises(ValidationError):
        DelegationDecision(workflow_name=name, rationale="x", confidence="high")


# 3, 4, 5. nothing but the three fields can be supplied
@pytest.mark.parametrize("field,value", [
    ("endpoint", "http://evil"), ("url", "http://evil"), ("service_url", "http://evil"),
    ("model_alias", "evil/model"), ("model", "gpt"), ("runtime_arn", "arn:aws:x"),
    ("services", ["research"]), ("steps", ["research"]), ("tools", ["shell"]),
    ("command", "rm -rf /"), ("credentials", "k"), ("api_key", "k"), ("identity_id", "orbit"),
])
def test_unknown_fields_are_rejected(field, value):
    with pytest.raises(ValidationError):
        DelegationDecision(workflow_name="full_review", rationale="x", confidence="high",
                           **{field: value})


def test_decision_schema_has_exactly_three_fields():
    assert list(DelegationDecision.model_fields) == ["workflow_name", "rationale", "confidence"]
    schema = DelegationDecision.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["workflow_name"]["enum"] == ["full_review", "research_security"]


@pytest.mark.parametrize("rationale", ["", "   ", "x" * 501])
def test_rationale_bounds(rationale):
    with pytest.raises(ValidationError):
        decision(rationale=rationale)
    assert decision(rationale="x" * 500)


def test_decision_is_immutable_and_json_round_trips_strictly():
    d = decision()
    with pytest.raises(ValidationError):
        d.workflow_name = "research_security"
    assert DelegationDecision.model_validate_json(d.model_dump_json()) == d
    with pytest.raises(ValidationError):
        DelegationDecision.model_validate_json(
            '{"workflow_name":"full_review","rationale":"x","confidence":"high","extra":1}')


# 9. validation uses the existing planner as the single source of truth
@pytest.mark.parametrize("workflow", ["full_review", "research_security"])
def test_approved_decisions_resolve_to_the_existing_plan(workflow):
    assert validate(decision(workflow)) == plan_workflow(
        workflow, default_bindings(), REGISTRY, ALL)


def test_delegable_workflows_are_a_subset_of_planned_workflows_with_security():
    assert DELEGABLE_WORKFLOWS == {"full_review", "research_security"} <= set(WORKFLOWS)
    for name in DELEGABLE_WORKFLOWS:
        assert Capability.SECURITY_ASSESSMENT in WORKFLOWS[name]


def test_confidence_and_rationale_do_not_affect_authorization():
    plans = {validate(decision("full_review", c, r)) for c in ("low", "high") for r in ("a", "b")}
    assert len(plans) == 1


# 8. invalid recommendations are rejected
@pytest.mark.parametrize("bad", [
    {"workflow_name": "full_review", "rationale": "x", "confidence": "high"},
    SimpleNamespace(workflow_name="full_review"), "full_review", None,
])
def test_non_decision_objects_are_rejected(bad):
    with pytest.raises(DelegationRejected, match="not a valid decision"):
        validate(bad)


def test_workflows_outside_the_allowlist_are_rejected():
    forged = DelegationDecision.model_construct(
        workflow_name="shell_access", rationale="x", confidence="high")
    with pytest.raises(DelegationRejected, match="not approved for delegation"):
        validate(forged)


def test_a_planned_workflow_is_not_delegable_until_allowlisted(monkeypatch):
    monkeypatch.setitem(WORKFLOWS, "research_only", (Capability.RESEARCH,))
    forged = DelegationDecision.model_construct(
        workflow_name="research_only", rationale="x", confidence="high")
    with pytest.raises(DelegationRejected, match="not approved for delegation"):
        validate(forged)


def test_the_security_assessment_is_mandatory(monkeypatch):
    from viharbot.execution_planner import ExecutionPlan, ExecutionStep

    only_research = ExecutionPlan(
        workflow_name="full_review",
        steps=(ExecutionStep(identity_id="nova", capability=Capability.RESEARCH,
                             service_name="research"),))
    monkeypatch.setattr(delegation, "plan_workflow", lambda *a, **k: only_research)
    with pytest.raises(DelegationRejected, match="security assessment is mandatory"):
        validate(decision())


# 9, 10. bindings and services stay authoritative
def test_disabled_binding_blocks_authorization():
    disabled = ExecutionBindingSet(
        [ExecutionBinding(identity_id=b.identity_id, capability=b.capability,
                          service_name=b.service_name, enabled=b.service_name != "synthesis")
         for b in default_bindings().bindings], REGISTRY)
    with pytest.raises(PlanningError, match="synthesis is disabled"):
        validate(decision("full_review"), disabled)
    assert validate(decision("research_security"), disabled)


def test_missing_services_block_authorization():
    with pytest.raises(PlanningError, match="privacy_review is not configured"):
        validate(decision("full_review"), available=ALL - {"privacy-review"})
    assert validate(decision("research_security"), available={"research", "security"})


# 8, 26. no network and no model clients during validation
def test_validation_makes_no_network_calls(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    assert len(validate(decision()).steps) == 6


def test_delegation_module_does_not_import_inference_clients():
    code = (
        "import sys\n"
        "from viharbot.delegation import DelegationDecision, validate_delegation\n"
        "from viharbot.execution_bindings import default_bindings\n"
        "from viharbot.identity_registry import default_registry\n"
        "decision = DelegationDecision(workflow_name='research_security', rationale='x',\n"
        "                              confidence='low')\n"
        "validate_delegation(decision, default_registry(), default_bindings(),\n"
        "                    {'research', 'security'})\n"
        "bad = [m for m in ('strands', 'litellm', 'boto3', 'openai') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
