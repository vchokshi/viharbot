from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from deadline_helpers import PLANNER_CONFIGURED_TIMEOUT, REMAINING_CASES, deadline_with
from orchestration_helpers import C, delegate, new_state
from viharbot import delegation_planner, model_agent, orchestration_planner
from viharbot.deadline import DeadlineExceeded
from viharbot.delegation import PlannerInferenceError
from viharbot.delegation_planner import PLANNER_MODEL_ENV, PLANNER_TIMEOUT_ENV
from viharbot.orchestration_planner import DecisionContext, decide_next_action


class Recorder:
    def __init__(self, on_build=None, on_invoke=None, error=None):
        self.built, self.invoked = [], []
        self.on_build, self.on_invoke, self.error = on_build, on_invoke, error

    def build_agent(self, **kwargs):
        self.built.append(kwargs)
        if self.on_build:
            self.on_build()

        def agent(message, *, structured_output_model):
            self.invoked.append(message)
            if self.on_invoke:
                self.on_invoke()
            if self.error:
                raise self.error
            return SimpleNamespace(structured_output=delegate(C.RESEARCH))

        return agent


@pytest.fixture
def planner(monkeypatch):
    monkeypatch.setenv(PLANNER_MODEL_ENV, "planner/alias")
    monkeypatch.delenv(PLANNER_TIMEOUT_ENV, raising=False)  # configured timeout is 30 s

    def blocked(*args, **kwargs):
        raise AssertionError("a real network call was attempted")

    monkeypatch.setattr(socket, "socket", blocked)

    def install(**kwargs):
        recorder = Recorder(**kwargs)
        monkeypatch.setattr(model_agent, "build_agent", recorder.build_agent)
        return recorder

    return install


def state():
    value = new_state(original_question="q")
    value.decision_number = 1
    return value


# the shared matrix: the planner gets min(configured 30 s, remaining), never rounded up
@pytest.mark.parametrize("remaining,expected,rest,agentcore", REMAINING_CASES)
def test_planner_timeout_is_min_of_configured_and_remaining(planner, remaining, expected, rest, agentcore):
    recorder = planner()
    deadline, _ = deadline_with(remaining)
    context = DecisionContext(deadline=deadline)
    if expected is None:
        with pytest.raises(DeadlineExceeded):
            decide_next_action(state(), context)
        assert recorder.built == [] and recorder.invoked == []  # no model inference
    else:
        assert decide_next_action(state(), context) == delegate(C.RESEARCH)
        assert recorder.built[0]["timeout"] == pytest.approx(expected)
        assert recorder.built[0]["max_retries"] == 0
        assert recorder.built[0]["timeout"] <= PLANNER_CONFIGURED_TIMEOUT


def test_a_short_allowance_is_never_rounded_up_to_one_second(planner):
    recorder = planner()
    deadline, _ = deadline_with(0.2)
    decide_next_action(state(), DecisionContext(deadline=deadline))
    assert recorder.built[0]["timeout"] == pytest.approx(0.2)
    assert recorder.built[0]["timeout"] < 1.0


def test_a_larger_remaining_allowance_is_capped_at_the_configured_timeout(planner):
    recorder = planner()
    deadline, _ = deadline_with(500.0)
    decide_next_action(state(), DecisionContext(deadline=deadline))
    assert recorder.built[0]["timeout"] == pytest.approx(30.0)


def test_prompt_preparation_time_reduces_the_allowance(planner, monkeypatch):
    recorder = planner()
    deadline, clock = deadline_with(5.0)
    original = orchestration_planner.build_decision_message

    def slow_prompt(*args, **kwargs):
        clock.now += 1.5  # building the digest takes 1.5 s
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestration_planner, "build_decision_message", slow_prompt)
    decide_next_action(state(), DecisionContext(deadline=deadline))
    assert recorder.built[0]["timeout"] == pytest.approx(3.5)


def test_preparation_that_uses_all_the_time_means_no_inference(planner, monkeypatch):
    recorder = planner()
    deadline, clock = deadline_with(1.0)
    original = orchestration_planner.build_decision_message
    monkeypatch.setattr(orchestration_planner, "build_decision_message",
                        lambda *a, **k: (setattr(clock, "now", clock.now + 2), original(*a, **k))[1])
    with pytest.raises(DeadlineExceeded):
        decide_next_action(state(), DecisionContext(deadline=deadline))
    assert recorder.invoked == []


def test_a_result_that_arrives_after_the_deadline_is_discarded(planner):
    deadline, clock = deadline_with(5.0)
    planner(on_invoke=lambda: setattr(clock, "now", clock.now + 6))
    with pytest.raises(DeadlineExceeded):
        decide_next_action(state(), DecisionContext(deadline=deadline))


# exception categories
def test_deadline_exceeded_is_not_wrapped_as_a_planner_inference_error(planner):
    deadline, _ = deadline_with(50.0)
    planner(error=DeadlineExceeded("request deadline exceeded"))
    with pytest.raises(DeadlineExceeded) as caught:
        decide_next_action(state(), DecisionContext(deadline=deadline))
    assert not isinstance(caught.value, PlannerInferenceError)


def test_a_client_timeout_after_expiry_is_reported_as_the_deadline(planner):
    deadline, clock = deadline_with(5.0)

    class Timeout(Exception):  # the shape of litellm.Timeout / httpx timeouts
        pass

    planner(on_invoke=lambda: setattr(clock, "now", clock.now + 6), error=Timeout("timed out"))
    with pytest.raises(DeadlineExceeded):
        decide_next_action(state(), DecisionContext(deadline=deadline))


def test_a_client_timeout_with_time_left_is_an_ordinary_planner_failure(planner):
    deadline, _ = deadline_with(50.0)

    class Timeout(Exception):
        pass

    planner(error=Timeout("upstream said SECRET"))
    with pytest.raises(PlannerInferenceError) as caught:
        decide_next_action(state(), DecisionContext(deadline=deadline))
    assert str(caught.value) == "planning inference failed" and "SECRET" not in str(caught.value)


def test_one_planning_call_with_no_retries(planner):
    recorder = planner()
    deadline, _ = deadline_with(30.0)
    decide_next_action(state(), DecisionContext(deadline=deadline))
    assert len(recorder.built) == len(recorder.invoked) == 1
    assert recorder.built[0]["max_retries"] == 0
    assert recorder.built[0]["model_alias"] == "planner/alias"  # still CHARLIE_PLANNER_MODEL


# compatibility
def test_the_legacy_timeout_seconds_field_is_still_honored_without_a_floor(planner):
    recorder = planner()
    decide_next_action(state(), DecisionContext(timeout_seconds=0.2))
    assert recorder.built[0]["timeout"] == pytest.approx(0.2)
    with pytest.raises(DeadlineExceeded):
        decide_next_action(state(), DecisionContext(timeout_seconds=0))


def test_without_any_deadline_the_configured_timeout_is_used(planner):
    recorder = planner()
    decide_next_action(state(), DecisionContext())
    assert recorder.built[0]["timeout"] == pytest.approx(30.0)


def test_phase_5_planning_is_unchanged_and_does_not_disable_retries(monkeypatch):
    seen = []

    def build_agent(**kwargs):
        seen.append(kwargs)
        return lambda message, *, structured_output_model: SimpleNamespace(
            structured_output=__import__("viharbot.delegation", fromlist=["x"]).DelegationDecision(
                workflow_name="full_review", rationale="r", confidence="high"))

    monkeypatch.setenv(PLANNER_MODEL_ENV, "planner/alias")
    monkeypatch.setattr(model_agent, "build_agent", build_agent)
    delegation_planner.recommend_workflow("q", "req-1")
    assert set(seen[0]) == {"model_alias", "system_prompt", "timeout"}  # no max_retries


def test_build_agent_passes_max_retries_only_when_asked(monkeypatch):
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.example.test")
    monkeypatch.setenv("LITELLM_API_KEY", "unit-test-key")
    captured = {}
    monkeypatch.setattr(model_agent, "LiteLLMModel", lambda **kw: captured.update(kw) or "model")
    monkeypatch.setattr(model_agent, "Agent", lambda **kw: kw)
    model_agent.build_agent(model_alias="a/b", system_prompt="p", timeout=0.2, max_retries=0)
    assert captured["client_args"]["timeout"] == 0.2 and captured["client_args"]["max_retries"] == 0
    model_agent.build_agent(model_alias="a/b", system_prompt="p", timeout=5)
    assert "max_retries" not in captured["client_args"]
    model_agent.build_agent(model_alias="a/b", system_prompt="p")
    assert set(captured["client_args"]) == {"api_base", "api_key", "use_litellm_proxy"}
