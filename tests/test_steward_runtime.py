"""The steward's AgentCore adapter: /ping and /invocations around one poll. Offline.

Pinned here: the strict input schema, refusal before any work, one poll at a time, results that
are counts and codes only, a crash that leaks nothing, a service that will not start on a bad
configuration, and AWS that is never contacted at startup.
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient
from steward_fakes import FakeCloudWatch
from test_steward_units import TOPIC, good_config

from viharbot.steward import aws, runtime
from viharbot.steward.config import ConfigError
from viharbot.steward.metrics import CloudWatchMetrics
from viharbot.steward.poller import PollResult
from viharbot.steward.runtime import AwsWiring, Invocation, create_app, create_app_from_env


class Recorder:
    def __init__(self, result: PollResult | None = None, error: Exception | None = None):
        self.calls: list[tuple[Invocation, str | None]] = []
        self.result = result or PollResult(status="ok", prs_assessed=1, alerts_sent=1,
                                           new_observations=3, pull_numbers=[179], duration_ms=12)
        self.error = error

    def __call__(self, invocation, request_id):
        self.calls.append((invocation, request_id))
        if self.error:
            raise self.error
        return self.result


def client(recorder=None, on_failure=None):
    recorder = recorder or Recorder()
    return TestClient(create_app(good_config(), recorder, on_failure)), recorder


def test_ping_reports_healthy():
    c, _ = client()
    assert c.get("/ping").json() == {"status": "Healthy"}


@pytest.mark.parametrize("body,action,pr,request_id", [
    ({}, "poll", None, None),
    ({"action": "poll"}, "poll", None, None),
    ({"input": {"action": "check", "pr": 179}, "request_id": "sched-1"}, "check", 179, "sched-1"),
    ({"input": {}}, "poll", None, None),
    ({"action": "check", "pr": 5}, "check", 5, None),
])
def test_valid_invocations_run_one_poll(body, action, pr, request_id):
    c, recorder = client()
    response = c.post("/invocations", json=body)
    assert response.status_code == 200
    ((invocation, seen_id),) = recorder.calls
    assert (invocation.action, invocation.pr, seen_id) == (action, pr, request_id)


def test_the_request_id_header_is_used_for_a_raw_payload():
    c, recorder = client()
    c.post("/invocations", json={"action": "poll"}, headers={"X-Request-Id": "hdr-9"})
    assert recorder.calls[0][1] == "hdr-9"


@pytest.mark.parametrize("body", [
    {"action": "delete"}, {"action": "poll", "extra": 1}, {"action": "poll", "pr": 5},
    {"action": "check"}, {"action": "check", "pr": 0}, {"action": "check", "pr": -3},
    {"action": "check", "pr": True}, {"action": "check", "pr": "179"}, {"action": "check", "pr": 10**9},
    {"action": "check", "pr": 1.5}, {"input": {"action": "poll"}, "request_id": "r", "extra": 1},
    {"input": "not a dict"}, {"input": ["list"]}, {"input": {"action": "poll"}, "request_id": 12},
    {"input": {"action": "poll"}, "request_id": "x" * 129}, {"repo": "other/repo"},
    {"input": {"action": "check", "pr": 179, "repo": "vchokshi/viharbot-onboarding-instructor"}},
])
def test_anything_else_is_refused_before_any_work(body):
    c, recorder = client()
    assert c.post("/invocations", json=body).status_code == 422
    assert recorder.calls == []


def test_a_body_that_is_not_a_json_object_is_refused():
    c, recorder = client()
    for content in (b"not json", b"[1,2]", b'"text"', b""):
        response = c.post("/invocations", content=content, headers={"content-type": "application/json"})
        assert response.status_code == 422
    assert recorder.calls == []


def test_an_oversized_body_is_refused_with_413():
    c, recorder = client()
    big = json.dumps({"action": "poll", "padding": "x" * 20_000})
    assert c.post("/invocations", content=big, headers={"content-type": "application/json"}).status_code == 413
    assert recorder.calls == []


def test_the_result_is_counts_and_codes_never_an_email_body():
    result = PollResult(status="partial", prs_assessed=2, alerts_sent=1, failures=1,
                        new_observations=4, error_code=None, dry_run=False, pull_numbers=[179, 180],
                        emails=[("Kepler: PR #179", "SECRET BODY with contributor text")])
    c, _ = client(Recorder(result))
    response = c.post("/invocations", json={})
    assert response.status_code == 200
    assert response.json() == result.public()
    assert "SECRET BODY" not in response.text and "emails" not in response.json()


def test_a_failed_poll_is_a_502_with_its_code_and_a_partial_one_is_a_200():
    failed = PollResult(status="failed", error_code="state")
    c, _ = client(Recorder(failed))
    response = c.post("/invocations", json={})
    assert response.status_code == 502 and response.json()["error_code"] == "state"
    partial = PollResult(status="partial", failures=1)
    c, _ = client(Recorder(partial))
    assert c.post("/invocations", json={}).status_code == 200


def test_a_crash_returns_a_fixed_body_tells_the_alarm_and_leaks_nothing(caplog):
    seen = []
    marker = "-----BEGIN " + "PRIVATE KEY-----"  # built here so the repo's key scanner stays quiet
    secret = RuntimeError(f"boom ghs_STOLENTOKEN {marker} contributor said hi")
    c, _ = client(Recorder(error=secret), on_failure=lambda: seen.append(1))
    caplog.set_level("DEBUG", logger="kepler")
    response = c.post("/invocations", json={})
    assert response.status_code == 500
    assert response.json() == {"status": "failed", "error_code": "internal"}
    assert seen == [1]
    for needle in ("ghs_", "PRIVATE KEY", "contributor said", "boom"):
        assert needle not in response.text and needle not in caplog.text
    ours = [json.loads(r.message) for r in caplog.records if r.name == "kepler"]  # not httpx's lines
    assert ours and ours[-1]["error_code"] == "internal" and ours[-1]["event"] == "invocation_crashed"


def test_only_one_poll_runs_at_a_time_in_a_container():
    started, release = threading.Event(), threading.Event()
    done = []

    def slow(invocation, request_id):
        started.set()
        release.wait(5)
        return PollResult(status="ok")

    app = create_app(good_config(), slow)

    def first():
        done.append(TestClient(app).post("/invocations", json={}).status_code)

    worker = threading.Thread(target=first)
    worker.start()
    assert started.wait(5)
    assert TestClient(app).post("/invocations", json={}).status_code == 409  # busy, not queued
    release.set()
    worker.join(5)
    assert done == [200]
    release.clear()
    started.clear()
    release.set()
    assert TestClient(app).post("/invocations", json={}).status_code == 200  # free again


def test_there_are_no_docs_or_schema_routes():
    c, _ = client()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404
    paths = {(m, r.path) for r in c.app.routes if hasattr(r, "methods") for m in r.methods}
    assert paths == {("GET", "/ping"), ("POST", "/invocations"), ("HEAD", "/ping")} or \
        paths == {("GET", "/ping"), ("POST", "/invocations")}


# ---- startup and AWS wiring ------------------------------------------------------------------------


def test_a_bad_configuration_stops_the_service_from_starting():
    with pytest.raises(ConfigError):
        create_app_from_env({})  # no topic
    with pytest.raises(ConfigError):
        create_app_from_env({"KEPLER_ALERT_TOPIC_ARN": TOPIC,
                             "KEPLER_REPOSITORY": "vchokshi/viharbot-onboarding-instructor"})
    with pytest.raises(ConfigError):
        create_app_from_env({"KEPLER_ALERT_TOPIC_ARN": TOPIC, "KEPLER_STATE_TABLE": "other-table"})


def test_startup_and_ping_never_contact_aws(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("AWS must not be contacted at startup or by /ping")

    monkeypatch.setattr(aws, "client", forbidden)
    c = TestClient(create_app_from_env({"KEPLER_ALERT_TOPIC_ARN": TOPIC}))
    assert c.get("/ping").status_code == 200


def test_the_real_wiring_is_built_once_on_first_use_and_passes_the_request_through(monkeypatch):
    built = []
    monkeypatch.setattr(aws, "secrets_client", lambda region: built.append("secrets") or object())
    monkeypatch.setattr(aws, "dynamodb_client", lambda region: built.append("dynamodb") or object())
    monkeypatch.setattr(aws, "sns_client", lambda region: built.append("sns") or object())
    monkeypatch.setattr(aws, "cloudwatch_client", lambda region: built.append("cloudwatch") or object())
    captured = []
    monkeypatch.setattr(runtime, "run_poll", lambda config, **kw: captured.append(kw) or PollResult(status="ok"))
    wiring = AwsWiring(good_config())
    assert built == []  # nothing is built until it is needed
    wiring.run(Invocation(action="check", pr=179), "req-1")
    wiring.run(Invocation(), None)
    assert sorted(built) == ["cloudwatch", "dynamodb", "secrets", "sns"]  # built once, reused
    assert captured[0]["only_pr"] == 179 and captured[0]["request_id"] == "req-1"
    assert captured[0]["dry_run"] is False
    assert captured[1]["only_pr"] is None and captured[1]["request_id"] == "scheduled"
    assert captured[0]["token_source"] is captured[1]["token_source"]  # the token cache is shared
    assert captured[0]["state"] is not captured[0]["audit"]


def test_a_crash_is_reported_to_the_failure_alarm_and_a_metrics_outage_is_swallowed(monkeypatch):
    cw = FakeCloudWatch()
    wiring = AwsWiring(good_config())
    wiring._deps = {"metrics": CloudWatchMetrics(cw)}
    wiring.note_internal_failure()
    assert cw.last("PollFailed") == 1
    cw.fail = True
    wiring.note_internal_failure()  # must not raise
    broken = AwsWiring(good_config())
    monkeypatch.setattr(aws, "secrets_client", lambda region: (_ for _ in ()).throw(RuntimeError("x")))
    broken.note_internal_failure()  # building the deps fails too; still must not raise
