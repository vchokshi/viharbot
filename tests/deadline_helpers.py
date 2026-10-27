"""Fakes for deterministic deadline tests: a controllable clock and in-memory transports."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

from orchestration_helpers import Clock
from pipeline_fixtures import RID, response
from viharbot.deadline import RequestDeadline

PLANNER_CONFIGURED_TIMEOUT = 30.0
REST_TIMEOUT_CAP = 90.0

# remaining seconds -> expected timeout for each transport (None means "no call, expired")
REMAINING_CASES = [
    (30.0, 30.0, 30.0, 30.0),
    (1.0, 1.0, 1.0, 1.0),
    (0.2, 0.2, 0.2, 0.2),
    (0.0, None, None, None),
]


def deadline_with(remaining: float, clock: Clock | None = None) -> tuple[RequestDeadline, Clock]:
    clock = clock or Clock()
    return RequestDeadline(expires_at=clock.now + remaining, clock=clock), clock


class FakeHTTPResponse:
    """A urlopen response whose body arrives in chunks; `on_read` runs before each read."""

    def __init__(self, body: bytes, on_read=None):
        self._buffer = io.BytesIO(body)
        self.on_read = on_read

    def read(self, amount: int | None = None) -> bytes:
        if self.on_read:
            self.on_read()
        return self._buffer.read() if amount is None else self._buffer.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_rest(monkeypatch, *, body: dict | None = None, on_open=None, on_read=None):
    """Replace demo.urlopen. Returns a list that records the timeout of each call."""
    from viharbot import demo

    calls: list[float] = []
    payload = json.dumps(body if body is not None else {**response("research"), "request_id": RID})

    def fake_urlopen(request, timeout):
        calls.append(timeout)
        if on_open:
            on_open()
        return FakeHTTPResponse(payload.encode(), on_read)

    monkeypatch.setattr(demo, "urlopen", fake_urlopen)
    return calls


class FakeStream:
    def __init__(self, body: bytes, on_read=None):
        self._buffer = io.BytesIO(body)
        self.on_read = on_read

    def read(self, amount: int | None = None) -> bytes:
        if self.on_read:
            self.on_read()
        return self._buffer.read() if amount is None else self._buffer.read(amount)


class FakeAgentCore:
    """A stand-in for boto3.session.Session that records every client configuration."""

    def __init__(self, *, body: dict | None = None, on_client=None, on_invoke=None, on_read=None):
        self.configs: list = []
        self.invocations: list[dict] = []
        self.sessions_created = 0
        self.body = body if body is not None else {**response("research"), "request_id": RID}
        self.on_client, self.on_invoke, self.on_read = on_client, on_invoke, on_read

    def session(self):
        outer = self
        outer.sessions_created += 1

        class Session:
            def client(self, name, region_name=None, config=None):
                outer.configs.append(config)
                if outer.on_client:
                    outer.on_client()
                return SimpleNamespace(
                    invoke_agent_runtime=lambda **kw: outer._invoke(config, kw))

        return Session()

    def _invoke(self, config, kwargs):
        self.invocations.append({"config": config, **kwargs})
        if self.on_invoke:
            self.on_invoke()
        return {"statusCode": 200,
                "response": FakeStream(json.dumps(self.body).encode(), self.on_read)}


def install_agentcore(monkeypatch, **kwargs) -> FakeAgentCore:
    from viharbot import agentcore_client

    fake = FakeAgentCore(**kwargs)
    monkeypatch.setattr(agentcore_client, "_session", None)
    monkeypatch.setattr(agentcore_client, "_make_session", fake.session)
    return fake
