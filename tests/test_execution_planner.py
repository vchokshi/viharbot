from __future__ import annotations

import socket

import pytest

from viharbot.execution_bindings import ExecutionBinding, ExecutionBindingSet, default_bindings
from viharbot.execution_planner import (
    PIPELINE_ORDER,
    WORKFLOWS,
    ExecutionPlan,
    ExecutionStep,
    PlanningError,
    plan_workflow,
    validate_dispatchable,
)
from viharbot.identity import Capability
from viharbot.identity_registry import load_registry

REGISTRY = load_registry()
ALL = set(PIPELINE_ORDER)


def plan(name="full_review", available=ALL, bindings=None):
    return plan_workflow(name, bindings or default_bindings(), REGISTRY, available)


def triples(p):
    return [(s.identity_id, s.capability.value, s.service_name) for s in p.steps]


# 8. full_review
def test_full_review_is_the_expected_six_step_plan():
    result = plan()
    assert result.workflow_name == "full_review"
    assert triples(result) == [
        ("nova", "research", "research"),
        ("scout", "fact_check", "fact-check"),
        ("scout", "source_quality", "source-quality"),
        ("cipher", "security_assessment", "security"),
        ("cipher", "privacy_review", "privacy-review"),
        ("charlie", "synthesis", "synthesis"),
    ]
    assert result.services == PIPELINE_ORDER


# 9. research_security
def test_research_security_is_the_expected_two_step_plan():
    result = plan("research_security", available={"research", "security"})
    assert triples(result) == [
        ("nova", "research", "research"),
        ("cipher", "security_assessment", "security"),
    ]


def test_workflow_set_is_fixed():
    assert set(WORKFLOWS) == {"full_review", "research_security"}


# 10. determinism
def test_planning_is_deterministic_and_plans_are_immutable():
    first, second = plan(), plan(available=list(ALL)[::-1])
    assert first == second
    with pytest.raises(ValueError):
        first.workflow_name = "other"
    with pytest.raises(AttributeError):
        first.steps.append(first.steps[0])


# 18. unsupported workflows
@pytest.mark.parametrize("name", [
    "", "unknown", "FULL_REVIEW", "full_review; research_security", "../full_review", None, 7,
])
def test_unsupported_workflows_fail_safely_without_echoing_input(name):
    with pytest.raises(PlanningError) as caught:
        plan(name)
    assert str(caught.value) == "unsupported workflow"


# 7. disabled / missing bindings
def test_disabled_binding_cannot_be_planned():
    bindings = ExecutionBindingSet(
        [ExecutionBinding(identity_id=b.identity_id, capability=b.capability,
                          service_name=b.service_name,
                          enabled=b.capability is not Capability.SECURITY_ASSESSMENT)
         for b in default_bindings().bindings],
        REGISTRY,
    )
    with pytest.raises(PlanningError, match="security_assessment is disabled"):
        plan(bindings=bindings)
    with pytest.raises(PlanningError, match="security_assessment is disabled"):
        plan("research_security", {"research", "security"}, bindings)
    # A workflow that does not use the disabled capability is unaffected.
    assert plan("research_security", {"research", "security"}, default_bindings())


def test_missing_binding_cannot_be_planned():
    only_research = ExecutionBindingSet(
        [ExecutionBinding(identity_id="nova", capability=Capability.RESEARCH,
                          service_name="research")], REGISTRY)
    with pytest.raises(PlanningError, match="no execution binding for capability fact_check"):
        plan(bindings=only_research)


def test_unconfigured_service_cannot_be_planned():
    with pytest.raises(PlanningError, match="synthesis is not configured"):
        plan(available=ALL - {"synthesis"})
    # research_security does not need the specialists.
    assert plan("research_security", {"research", "security"})


def test_planned_only_identities_never_appear_in_a_plan():
    identities = {s.identity_id for name in WORKFLOWS for s in plan(name).steps}
    assert identities.isdisjoint({"atlas", "pixel", "sage", "orbit"})


# dispatch validation
def step(service, capability=Capability.RESEARCH):
    return ExecutionStep(identity_id="nova", capability=capability, service_name=service)


@pytest.mark.parametrize("services", [
    ("security", "research"),
    ("research", "synthesis", "security"),
    ("research",),
    ("fact-check", "security"),
    ("research", "research", "security"),
    ("research", "mystery", "security"),
])
def test_plans_the_pipeline_cannot_run_are_rejected(services):
    bad = ExecutionPlan(workflow_name="x", steps=tuple(step(s) for s in services))
    with pytest.raises(PlanningError):
        validate_dispatchable(bad)


# 19. no network
def test_planning_makes_no_network_calls(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    assert len(plan().steps) == 6
