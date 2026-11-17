"""Runtime checks around governed memory (issue #40): Charlie's call order and degraded operation,
that no other agent gains memory, binding/Stage independence and the single inference path.

Offline: the model is stubbed at ``build_agent``; the only network is 127.0.0.1. Nothing here
contacts DigitalOcean, AWS or a gateway, and nothing is deployed.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from charlie_memory_helpers import (
    ROOT,
    FakeMemoryHttp,
    FakeRepo,
    LogCapture,
    charlie,
    cleanup_module,
    free_port,
    memory_env,
    standard_result,
)
from memory_helpers import build_world

from viharbot import charlie_memory as cm
from viharbot import memory_client as mc
from viharbot.memory_service.policy import load_policy

SRC = ROOT / "src" / "viharbot"
QUESTION = "SECRET-QUESTION-77aa: what do we tell Acme?"
ANSWER = "SECRET-ANSWER-31cc"
LIST_OK = b'{"kind":"memory_record_list","not_instructions":true,"records":[]}'


@pytest.fixture(autouse=True)
def _fresh_module():
    yield
    cleanup_module()


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path, FakeRepo(), extra_subs=("charlie", "web"))


def _env(world, url: str, **extra: str) -> dict[str, str]:
    return memory_env(url, str(world.keys["charlie"][1]), VIHARBOT_MEMORY_TIMEOUT_SECONDS="0.5", **extra)


# --- Charlie: read before synthesis, write after ----------------------------------------------


def test_charlie_reads_before_the_model_runs_and_writes_after_it(monkeypatch, tmp_path, world):
    events: list[str] = []
    seen = charlie(monkeypatch, tmp_path, _env(world, "http://127.0.0.1:9"), result=standard_result(ANSWER))
    monkeypatch.setattr(mc.MemoryClient, "recent", lambda self, rid, agent="x": events.append("read") or [])
    monkeypatch.setattr(mc.MemoryClient, "write", lambda self, c, rid, agent="x": events.append("write") or "stored")
    inner = seen.module.build_agent

    def build(**kwargs):
        run = inner(**kwargs)

        def spy(prompt, structured_output_model=None):
            events.append("model")
            return run(prompt, structured_output_model=structured_output_model)

        return spy

    monkeypatch.setattr(seen.module, "build_agent", build)
    assert seen.ask(QUESTION).status_code == 200
    assert events == ["read", "model", "write"]


def test_the_write_is_attempted_once_per_request_even_when_the_read_failed(monkeypatch, tmp_path, world):
    with FakeMemoryHttp(status=503, body=b"{}") as fake, LogCapture() as logs:
        seen = charlie(monkeypatch, tmp_path, _env(world, fake.url))
        assert seen.ask(QUESTION).status_code == 200
    assert [e["operation"] for e in logs.memory_events() if e["operation"] != "startup"] == ["read", "write"]
    assert [r.method for r in fake.seen].count("GET") == 1


# --- degraded operation: exactly one content-free line per operation ---------------------------


DEGRADED = [
    ("down", None, "unreachable"),
    ("503", dict(status=503, body=b'{"detail":"x"}'), "http_503"),
    ("401", dict(status=401, body=b'{"detail":"x"}'), "denied"),
    ("403", dict(status=403, body=b'{"detail":"x"}'), "denied"),
]


@pytest.mark.parametrize("name, script, reason", DEGRADED, ids=[d[0] for d in DEGRADED])
def test_memory_trouble_gives_one_log_line_per_operation_and_an_ordinary_answer(
        monkeypatch, tmp_path, world, name, script, reason):
    def run(url: str):
        with LogCapture() as logs:
            seen = charlie(monkeypatch, tmp_path, _env(world, url), result=standard_result(ANSWER))
            response = seen.ask(QUESTION)
        return seen, response, logs

    if script is None:
        seen, response, logs = run(f"http://127.0.0.1:{free_port()}")
        requests = []
    else:
        with FakeMemoryHttp(**script) as fake:
            seen, response, logs = run(fake.url)
        requests = fake.seen
    assert response.status_code == 200 and response.json()["result"]["answer"] == ANSWER
    assert "UNTRUSTED PRIOR MEMORY" not in seen.calls[0]["user"]
    events = [e for e in logs.memory_events() if e["operation"] != "startup"]
    assert [(e["operation"], e["decision"], e["reason"]) for e in events] == [
        ("read", "skipped", reason), ("write", "skipped", reason)]
    lines = [line for line in logs.lines() if '"event":"memory client"' in line and '"operation":"startup"' not in line]
    assert len(lines) == 2, lines
    everything = logs.everything()
    for secret in (QUESTION, ANSWER, "SECRET-"):
        assert secret not in everything
    if name in ("401", "403"):  # a refusal is final: the write is not retried
        assert [r.method for r in requests] == ["GET", "POST"]
    if name == "503":  # transient: the write is retried once, with the same idempotency key
        assert [r.method for r in requests] == ["GET", "POST", "POST"]
        keys = {r.body for r in requests if r.method == "POST"}
        assert len(keys) == 1


def test_a_tls_failure_toward_a_plain_http_peer_is_one_content_free_line_each(monkeypatch, tmp_path, world):
    with FakeMemoryHttp(status=200, body=LIST_OK) as fake, LogCapture() as logs:
        url = fake.url.replace("http://", "https://")  # the peer speaks plain HTTP: handshake fails
        env = _env(world, url)
        env.pop("VIHARBOT_MEMORY_LOCAL_DEV")
        seen = charlie(monkeypatch, tmp_path, env, result=standard_result(ANSWER))
        response = seen.ask(QUESTION)
    assert response.status_code == 200 and response.json()["result"]["answer"] == ANSWER
    events = [e for e in logs.memory_events() if e["operation"] != "startup"]
    assert [(e["operation"], e["decision"]) for e in events] == [("read", "skipped"), ("write", "skipped")]
    assert all(re.fullmatch(r"[a-z0-9_]+", e["reason"] or "") for e in events)
    assert QUESTION not in logs.everything()


def test_a_failed_model_call_does_not_write_and_is_still_a_normal_error(monkeypatch, tmp_path, world):
    with FakeMemoryHttp(status=200, body=LIST_OK) as fake:
        seen = charlie(monkeypatch, tmp_path, _env(world, fake.url))

        def boom(**kwargs):
            def run(prompt, structured_output_model=None):
                raise RuntimeError("SECRET-MODEL-DETAIL")
            return run

        seen.module.build_agent = boom
        response = seen.ask(QUESTION)
    assert response.status_code >= 500
    assert "SECRET-MODEL-DETAIL" not in response.text
    assert [r.method for r in fake.seen] == ["GET"]  # read happened, nothing was written


# --- no other agent gains memory ----------------------------------------------------------------


@pytest.mark.parametrize("kind", ["fact-check", "source-quality", "privacy-review"])
def test_other_specialists_make_no_memory_request_even_with_a_url_configured(monkeypatch, tmp_path, world, kind):
    with FakeMemoryHttp(status=200, body=LIST_OK) as fake, LogCapture() as logs:
        seen = charlie(monkeypatch, tmp_path, _env(world, fake.url), agent_kind=kind)
        assert seen.module._memory is None
        seen.ask(QUESTION)
    assert fake.seen == []
    assert [(e["decision"], e["reason"]) for e in logs.memory_events("startup")] == [
        ("skipped", "agent_not_integrated")]
    assert logs.memory_events("read") == [] and logs.memory_events("write") == []


@pytest.mark.parametrize("agent", ["nova", "research", "scout", "cipher", "security", "pixel", "sage", "web", "kepler"])
def test_load_client_gives_nothing_to_any_agent_but_synthesis(agent):
    env = {mc.URL_ENV: "http://127.0.0.1:9"}
    assert cm.load_client(agent, env) is None


def test_only_the_synthesis_path_references_the_memory_client_or_its_settings():
    allowed = {"memory_client.py", "charlie_memory.py", "specialist_agent.py"}
    offenders = []
    for path in SRC.rglob("*.py"):
        if "memory_service" in path.parts or path.name in allowed:
            continue
        text = path.read_text()
        if re.search(r"memory_client|charlie_memory|VIHARBOT_MEMORY|MemoryClient", text):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


def _compose_service_blocks() -> dict[str, str]:
    text = (ROOT / "compose.yaml").read_text()
    services = text.split("\nservices:\n", 1)[1].split("\nnetworks:", 1)[0].split("\nvolumes:", 1)[0]
    parts = re.split(r"(?m)^  ([a-z][a-z-]*):\s*$", services)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_only_the_synthesis_service_is_given_memory_settings_in_compose():
    blocks = _compose_service_blocks()
    assert {"web", "research", "synthesis", "pixel", "sage", "orchestrator"} <= set(blocks)
    holders = {name for name, body in blocks.items() if re.search(r"VIHARBOT_MEMORY_[A-Z_]+:", body)}
    assert holders == {"synthesis"}
    for name, body in blocks.items():
        if name not in ("synthesis", "memory", "memory-migrate", "postgres"):
            assert "memory-client" not in body and "viharbot-memory" not in body, name


def test_the_shipped_memory_policy_is_a_pinned_snapshot_and_charlie_has_no_session_tier():
    policy = load_policy()
    raw = tomllib.loads((SRC / "execution_config" / "memory_policy.toml").read_text())
    assert [a["agent_id"] for a in raw["agent"]] == ["nova", "scout", "cipher", "charlie", "pixel", "sage"]
    by_id = {a["agent_id"]: a for a in raw["agent"]}
    assert by_id["charlie"]["session"] == []
    for agent in raw["agent"]:
        assert agent["subs"] == [agent["agent_id"]]
        assert agent["classification_ceiling"] == "internal"
        assert agent["allow_personal_data"] is False
        assert agent["max_content_bytes"] <= 16384
    # Being named in the policy is necessary, not sufficient: the registry must also grant the
    # capability (see the registry tests below). Charlie may touch only its own agent-private scope.
    assert policy.for_sub("charlie").agent_id == "charlie"


def test_no_shipped_key_registry_names_memory_capabilities_for_anyone_but_charlie():
    for path in list(ROOT.rglob("*registry*.toml")) + list((ROOT / "scripts").glob("*.py")):
        if ".git" in path.parts or "tests" in path.parts:
            continue
        text = path.read_text()
        if "memory.read" not in text and "memory.write" not in text:
            continue
        subs = set(re.findall(r'sub\s*=\s*"([a-z-]+)"', text))
        assert subs <= {"charlie"}, (str(path), subs)


@pytest.mark.parametrize("agent", ["nova", "scout", "cipher", "pixel", "sage", "web"])
def test_an_agent_key_with_a_non_memory_capability_is_refused_by_the_memory_service(tmp_path, agent):
    """An agent key that carries another route capability (not memory.read or memory.write) gets nothing."""
    from memory_helpers import agent_path, create_body

    from viharbot import service_auth as sa

    world = build_world(tmp_path, FakeRepo(), extra_subs=("web", "pixel", "sage"))
    for method, cap, body in (("GET", "agent.invoke", None), ("POST", "agent.invoke", create_body("x"))):
        payload = b"" if body is None else json.dumps(body).encode()
        token = world.token(agent, method, agent_path(agent), payload, cap=cap)
        response = world.call(None, method, agent_path(agent), body, token=token)
        assert response.status_code in (401, 403)
    assert world.repo.records == [] and sa.MEMORY_READ_CAPABILITY == "memory.read"


# --- bindings, routing and Stage independence --------------------------------------------------


def _stage_blocks() -> dict[str, str]:
    text = (ROOT / "deploy" / "digitalocean" / "app.yaml").read_text()
    parts = re.split(r"(?m)^  - name: ([a-z-]+)\s*$", text.split("\nservices:\n", 1)[1])
    return {parts[i]: parts[i + 1] for i in range(0, len(parts) - 1, 2)} if len(parts) % 2 == 0 else {
        parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


STAGE_AGENTS = ("research", "security", "fact-check", "source-quality", "privacy-review", "synthesis")


def test_every_stage_agent_is_its_own_service_with_its_own_image_health_and_no_shared_state():
    blocks = _stage_blocks()
    assert set(blocks) == {"web", *STAGE_AGENTS}
    dockerfiles = []
    for name in STAGE_AGENTS:
        body = blocks[name]
        assert "http_path: /health" in body, name
        assert "deploy_on_push: false" in body, name
        assert "internal_ports" in body and "http_port" not in body, name  # never public
        dockerfiles.append(re.search(r"dockerfile_path: (\S+)", body).group(1))
        for other in STAGE_AGENTS:
            if other != name:
                assert f"http://{other}:" not in body, f"{name} must not call {other}"
    assert "deploy_on_push: false" in blocks["web"]
    # research and security have their own image; the four specialists share one by design.
    assert dockerfiles[0] != dockerfiles[1] and len(set(dockerfiles[2:])) == 1


def test_the_web_service_routes_to_each_stage_agent_by_internal_name():
    web = _stage_blocks()["web"]
    urls = dict(re.findall(r"key: ([A-Z_]+_URL)\s+value: (\S+)", web))
    expected = {
        "RESEARCH_URL": "research", "SECURITY_URL": "security", "FACT_CHECK_URL": "fact-check",
        "SOURCE_QUALITY_URL": "source-quality", "PRIVACY_REVIEW_URL": "privacy-review",
        "SYNTHESIS_URL": "synthesis",
    }
    for key, service in expected.items():
        assert urls[key].startswith(f"http://{service}:8000/"), key


def test_bindings_for_stage_services_name_services_that_exist_and_pixel_and_sage_stay_dev_only():
    stage = set(_stage_blocks())
    raw = tomllib.loads((SRC / "execution_config" / "bindings.toml").read_text())
    bound = {b["service_name"] for b in raw["binding"]}
    stage_bound = {"research", "fact-check", "source-quality", "security", "privacy-review", "synthesis"}
    assert stage_bound <= bound and stage_bound <= stage
    assert not ({"pixel", "sage", "sage-code-review"} & stage)


@pytest.mark.parametrize("kind", ["fact-check", "source-quality", "privacy-review", "synthesis"])
def test_specialist_health_reports_the_agent_and_needs_no_memory(monkeypatch, tmp_path, kind):
    seen = charlie(monkeypatch, tmp_path, {}, agent_kind=kind)
    response = seen.client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok", "agent": kind}


def test_health_does_not_touch_memory_even_when_memory_is_down(monkeypatch, tmp_path, world):
    seen = charlie(monkeypatch, tmp_path, _env(world, f"http://127.0.0.1:{free_port()}"))
    with LogCapture() as logs:
        assert seen.client.get("/health").status_code == 200
    assert logs.memory_events() == []


# --- one inference path --------------------------------------------------------------------------


PROVIDER_MARKERS = re.compile(
    r"(?m)^\s*(?:import|from)\s+(?:openai|anthropic|boto3|botocore|google\.generativeai|google\.genai|"
    r"vertexai|mistralai|cohere|ollama)\b|api\.openai\.com|api\.anthropic\.com|bedrock-runtime|"
    r"generativelanguage\.googleapis\.com"
)
# The only allowed users: Kepler's steward (ADR-011 exception) and the AgentCore runtime client
# (invokes a deployed agent runtime; it is not model inference).
PROVIDER_ALLOWED = {"steward", "agentcore_client.py", "nova_routing.py"}


def test_no_provider_native_inference_outside_the_kepler_exception():
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC)
        if rel.parts[0] in PROVIDER_ALLOWED or rel.name in PROVIDER_ALLOWED:
            continue
        if PROVIDER_MARKERS.search(path.read_text()):
            offenders.append(str(rel))
    assert offenders == []


def test_agentcore_client_never_calls_a_model_api():
    text = (SRC / "agentcore_client.py").read_text()
    assert "bedrock-runtime" not in text and "invoke_model" not in text and "converse" not in text.lower()


def test_every_model_using_service_builds_its_agent_through_model_agent():
    users = ["specialist_agent", "research_agent", "security_agent", "pixel_agent", "sage_agent"]
    for name in users:
        text = (SRC / f"{name}.py").read_text()
        assert re.search(r"from \.model_agent import .*build_agent|from \.model_agent import \(", text), name
    text = (SRC / "model_agent.py").read_text()
    assert "use_litellm_proxy" in text and "litellm_credentials" in text
