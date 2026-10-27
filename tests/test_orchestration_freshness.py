from __future__ import annotations

import pytest

from orchestration_helpers import C, Script, delegate, finish, make_step, new_state, with_steps
from pipeline_fixtures import FINDINGS, RID, FakeSend
from viharbot.execution_bindings import default_bindings
from viharbot.identity_registry import default_registry
from viharbot.iterative_execution import OrchestrationFailed, run_iterative_pipeline
from viharbot.orchestration_policy import (
    ActionRejected, OrchestrationAction, authorize_action, check_completion, fresh_latest,
    fresh_results, input_versions, step_is_fresh)
from viharbot.orchestration_state import ExecutionBudget

R, FC, SQ, S, P, Y = (C.RESEARCH, C.FACT_CHECK, C.SOURCE_QUALITY, C.SECURITY_ASSESSMENT,
                      C.PRIVACY_REVIEW, C.SYNTHESIS)
SERVICES = {"research", "security", "fact-check", "source-quality", "privacy-review", "synthesis"}
URLS = {"fact_check": "u-f", "source_quality": "u-s", "privacy_review": "u-p", "synthesis": "u-y"}
BIG = ExecutionBudget(max_decisions=12, max_executions=10)
NAMES = {FC: "fact_check", SQ: "source_quality", P: "privacy_review", Y: "synthesis"}


class VersionedSend(FakeSend):
    """Every research call returns a different summary so versions are distinguishable."""

    def __init__(self):
        self.research_calls = 0
        super().__init__({"research": self._versioned})

    def _versioned(self, raw):
        self.research_calls += 1
        return {**raw, "findings": {**FINDINGS, "summary": f"research-v{self.research_calls}"}}


def run(*capabilities, end=finish(), send=None, budget=BIG):
    actions = [delegate(c) for c in capabilities] + ([end] if end else [])
    send = send or VersionedSend()
    result = run_iterative_pipeline(
        "q", RID, research_url="u-r", security_url="u-sec", specialist_urls=URLS, send=send,
        decide=Script(*actions), budget=budget)
    return result, send


def authorize(capability, state):
    return authorize_action(delegate(capability), state, default_registry(), default_bindings(), SERVICES)


# the freshness rule itself
def test_mandatory_results_are_fresh_when_built_on_the_latest_research():
    state = with_steps(make_step(R), make_step(S, 1, {"research": 1}))
    assert step_is_fresh(state, state.latest(S)) and check_completion(state) is None
    assert set(fresh_results(state)) == {R, S}


def test_security_is_stale_once_research_is_repeated():
    state = with_steps(make_step(R), make_step(S, 1, {"research": 1}), make_step(R, 2))
    assert not step_is_fresh(state, state.latest(S))
    assert check_completion(state) == "security_stale"
    assert set(fresh_results(state)) == {R}  # a stale mandatory result is not offered either


@pytest.mark.parametrize("optional,hard,soft", [
    (FC, {"research": 1}, {}), (SQ, {"research": 1}, {}),
    (P, {"research": 1, "security_assessment": 1}, {}),
    (Y, {"research": 1, "security_assessment": 1}, {}),
])
def test_an_optional_result_is_stale_when_a_required_input_changes(optional, hard, soft):
    state = with_steps(make_step(R), make_step(S, 1, {"research": 1}),
                       make_step(optional, 1, {**hard, **soft}))
    assert step_is_fresh(state, state.latest(optional))
    state.completed_steps.append(make_step(R, 2))
    assert not step_is_fresh(state, state.latest(optional))
    assert optional not in fresh_results(state)


def test_a_newer_version_of_a_consumed_optional_input_makes_the_result_stale():
    state = with_steps(make_step(R), make_step(S, 1, {"research": 1}),
                       make_step(FC, 1, {"research": 1}),
                       make_step(Y, 1, {"research": 1, "security_assessment": 1, "fact_check": 1}))
    assert step_is_fresh(state, state.latest(Y))
    state.completed_steps.append(make_step(FC, 2, {"research": 1}))
    assert not step_is_fresh(state, state.latest(Y)) and step_is_fresh(state, state.latest(FC))


def test_an_optional_input_that_appeared_after_the_run_makes_the_result_stale():
    state = with_steps(make_step(R), make_step(S, 1, {"research": 1}),
                       make_step(Y, 1, {"research": 1, "security_assessment": 1}))
    assert step_is_fresh(state, state.latest(Y))
    state.completed_steps.append(make_step(FC, 1, {"research": 1}))
    assert not step_is_fresh(state, state.latest(Y))  # strict: newer evidence exists


def test_a_stale_optional_input_is_ignored_when_judging_a_newer_result():
    state = with_steps(make_step(R), make_step(FC, 1, {"research": 1}), make_step(R, 2),
                       make_step(S, 1, {"research": 2}),
                       make_step(Y, 1, {"research": 2, "security_assessment": 1}))
    assert fresh_latest(state, FC) is None
    assert step_is_fresh(state, state.latest(Y))  # it correctly left the stale fact check out


def test_input_versions_skip_stale_optional_inputs():
    state = with_steps(make_step(R), make_step(FC, 1, {"research": 1}), make_step(R, 2),
                       make_step(S, 1, {"research": 2}))
    assert input_versions(Y, state) == {"research": 2, "security_assessment": 1}


# authorization: stale required dependencies block execution
@pytest.mark.parametrize("capability", [P, Y])
def test_downstream_execution_is_rejected_when_security_is_stale(capability):
    state = with_steps(make_step(R), make_step(S, 1, {"research": 1}), make_step(R, 2))
    with pytest.raises(ActionRejected) as caught:
        authorize(capability, state)
    assert caught.value.code == "dependency_stale"
    state.completed_steps.append(make_step(S, 2, {"research": 2}))
    assert authorize(capability, state).capability is capability


def test_rejected_downstream_execution_never_calls_a_service():
    send = VersionedSend()
    script = Script(delegate(R), delegate(S), delegate(R), delegate(P), delegate(Y), delegate(S),
                    finish())
    budget = ExecutionBudget(max_decisions=12, max_executions=10, max_rejected_actions=3)
    result = run_iterative_pipeline("q", RID, research_url="u-r", security_url="u-sec",
                                    specialist_urls=URLS, send=send, decide=script, budget=budget)
    assert send.order == ["research", "security", "research", "security"]
    rejected = [r.reason_code for r in result.orchestration.decision_history if r.status == "rejected"]
    assert rejected == ["dependency_stale", "dependency_stale"]


def test_fact_check_and_source_quality_only_need_the_latest_research():
    state = with_steps(make_step(R), make_step(S, 1, {"research": 1}), make_step(R, 2))
    assert authorize(FC, state).capability is FC and authorize(SQ, state).capability is SQ


# the final response excludes stale optional results
@pytest.mark.parametrize("optional,prefix", [
    (Y, (R, S, Y)), (FC, (R, FC, S)), (SQ, (R, SQ, S)), (P, (R, S, P)),
])
def test_stale_optional_results_are_left_out_of_the_final_response(optional, prefix):
    result, send = run(*prefix, R, S)
    legacy = result.to_legacy_dict()
    key = {FC: "fact_check", SQ: "source_quality", P: "privacy_review", Y: "synthesis"}[optional]
    assert key not in legacy  # not presented as current, and nothing replaced it
    assert legacy["research"]["findings"]["summary"] == "research-v2"
    assert legacy["security"]["assessment"]
    assert result.orchestration.status == "completed"
    assert result.orchestration.latest(optional) is not None  # still kept as evidence
    assert [a["agent"] for a in legacy["agent_activity"]].count("research") == 2  # history intact
    assert send.research_calls == 2  # nothing was silently rerun


def test_a_refreshed_optional_result_is_included_again():
    result, _ = run(R, S, Y, R, S, Y)
    legacy = result.to_legacy_dict()
    assert legacy["synthesis"]["result"]["answer"] and list(legacy)[-1] == "synthesis"
    assert result.orchestration.latest(Y).version == 2


def test_fresh_optional_results_survive_beside_stale_ones():
    result, _ = run(R, FC, SQ, S, R, S, FC)
    legacy = result.to_legacy_dict()
    assert "fact_check" in legacy and "source_quality" not in legacy


def test_optional_dependency_changes_make_a_synthesis_stale_in_the_loop():
    consumed_then_changed, _ = run(R, S, FC, Y, FC)
    assert "synthesis" not in consumed_then_changed.to_legacy_dict()
    assert "fact_check" in consumed_then_changed.to_legacy_dict()
    appeared_later, _ = run(R, S, Y, FC)
    assert "synthesis" not in appeared_later.to_legacy_dict()


def test_stale_optional_evidence_is_not_fed_into_later_payloads():
    result, send = run(R, FC, S, R, S, Y)
    context = send.payload("synthesis")["context"]
    assert list(context) == ["research", "security", "privacy_review"]  # no stale fact check
    assert context["research"]["summary"] == "research-v2"
    synthesis = result.orchestration.latest(Y)
    assert "fact_check" not in synthesis.based_on and synthesis.based_on["research"] == 2
    assert "synthesis" in result.to_legacy_dict()  # and it is correctly judged fresh


def test_repeated_research_and_security_record_the_versions_later_results_used():
    result, _ = run(R, R, FC, S, S, P)
    steps = {(s.capability, s.version): s for s in result.orchestration.completed_steps}
    assert steps[(FC, 1)].based_on == {"research": 2}
    assert steps[(S, 1)].based_on == {"research": 2} and steps[(S, 2)].based_on == {"research": 2}
    assert steps[(P, 1)].based_on["security_assessment"] == 2
    assert result.orchestration.status == "completed"


def test_finish_with_stale_security_is_rejected_and_never_hidden():
    with pytest.raises(OrchestrationFailed) as caught:
        run_iterative_pipeline(
            "q", RID, research_url="u-r", security_url="u-sec", specialist_urls=URLS,
            send=VersionedSend(), budget=BIG,
            decide=Script(delegate(R), delegate(S), delegate(R), finish(), finish()))
    assert caught.value.code == "rejection_limit"


def test_refreshing_security_allows_completion_with_current_results():
    result, _ = run(R, S, R, S)
    assert result.orchestration.status == "completed"
    assert result.to_legacy_dict()["security"]["assessment"]
    assert result.orchestration.latest(S).based_on == {"research": 2}


def test_the_public_response_shape_is_unchanged_by_exclusions():
    from viharbot import web_app

    result, _ = run(R, S, Y, R, S)
    body = result.to_legacy_dict()
    chat = web_app.ChatResponse.model_validate({
        "request_id": body["request_id"], "findings": body["research"]["findings"],
        "assessment": body["security"]["assessment"], "fact_check": None,
        "source_quality": None, "privacy_review": None, "synthesis": None,
        "agent_activity": body["agent_activity"]})
    assert chat.synthesis is None and chat.findings.summary == "research-v2"
