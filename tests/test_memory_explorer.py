"""Agent Memory Explorer, phase 1: auth, registry-driven agents, the working-memory projection,
isolation, injection safety, the portrait entry point and Command Central configuration.
Offline: no model, gateway, database or network call."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestration_helpers import C, make_step, new_state, with_steps
from viharbot import web_app, working_memory
from viharbot.identity_registry import default_registry
from viharbot.orchestration_state import DecisionRecord
from viharbot.team_profiles import render_profiles, team_profiles

META = "m" * 32
CONTENT = "c" * 32
SECRET_QUESTION = "SECRET-QUESTION-TEXT"
STATIC = Path(web_app.__file__).with_name("static")


@pytest.fixture(autouse=True)
def explorer(monkeypatch):
    monkeypatch.setenv("MEMORY_EXPLORER_ENABLED", "true")
    monkeypatch.setenv("MEMORY_EXPLORER_FOUNDER_TOKEN", META)
    monkeypatch.setenv("MEMORY_EXPLORER_CONTENT_TOKEN", CONTENT)
    monkeypatch.delenv("COMMAND_CENTRAL_URL", raising=False)
    monkeypatch.delenv("MEMORY_EXPLORER_BASE_URL", raising=False)
    monkeypatch.delenv("WEB_AUTH_ENABLED", raising=False)
    working_memory.registry.clear()
    yield
    working_memory.registry.clear()


client = TestClient(web_app.app)


def auth(token=META):
    return {"Authorization": f"Bearer {token}"}


def tracked_state(**kwargs):
    state = with_steps(make_step(C.RESEARCH), make_step(C.SECURITY_ASSESSMENT),
                       original_question=SECRET_QUESTION, started_at=time.monotonic(), **kwargs)
    state.decision_number = 3
    state.iteration = 2
    state.record(DecisionRecord(decision_number=1, requested_action="delegate",
                                capability=C.RESEARCH, status="executed", duration_ms=5.0))
    state.record(DecisionRecord(decision_number=3, requested_action="delegate",
                                capability=C.SYNTHESIS, status="rejected", reason_code="missing_dependency"))
    working_memory.registry.track(state)
    return state


# ---- authorization: deny by default ----------------------------------------------------------

def test_disabled_by_default_returns_unavailable(monkeypatch):
    monkeypatch.setenv("MEMORY_EXPLORER_ENABLED", "false")
    assert client.get("/api/memory-explorer/status").json() == {"state": "unavailable"}
    assert client.get("/api/memory-explorer/agents", headers=auth()).status_code == 503


@pytest.mark.parametrize("path", [
    "/api/memory-explorer/config", "/api/memory-explorer/agents",
    "/api/memory-explorer/agents/charlie", "/api/memory-explorer/agents/charlie/working-memory",
    "/api/memory-explorer/agents/charlie/persistent-memory", "/api/memory-explorer/agents/charlie/events"])
def test_every_api_denies_missing_and_wrong_credentials(path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get(path, headers={"Authorization": f"Basic {META}"}).status_code == 401
    assert client.get(path, headers=auth()).status_code == 200


def test_short_or_missing_tokens_fail_closed(monkeypatch):
    monkeypatch.setenv("MEMORY_EXPLORER_FOUNDER_TOKEN", "short")
    monkeypatch.delenv("MEMORY_EXPLORER_CONTENT_TOKEN")
    r = client.get("/api/memory-explorer/agents", headers=auth("short"))
    assert r.status_code == 503


def test_shell_holds_no_data_and_status_is_content_free():
    tracked_state()
    assert SECRET_QUESTION not in client.get("/agents/charlie").text
    assert SECRET_QUESTION not in client.get("/api/memory-explorer/status").text


def test_shell_respects_site_auth(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "true")
    monkeypatch.setenv("WEB_USERNAME", "u")
    monkeypatch.setenv("WEB_PASSWORD", "p")
    assert client.get("/agents/charlie").status_code == 401


def test_invalid_agent_id_is_rejected():
    for bad in ("nobody", "..%2Fetc", "CHARLIE", "a" * 40):
        r = client.get(f"/api/memory-explorer/agents/{bad}", headers=auth())
        assert r.status_code == 404
    assert client.get("/agents/Bad Id").status_code in (404, 422)


# ---- registry-driven agents -------------------------------------------------------------------

def test_agent_list_comes_from_registry_and_includes_unbound_identities():
    agents = client.get("/api/memory-explorer/agents", headers=auth()).json()["agents"]
    assert [a["id"] for a in agents] == [i.id for i in default_registry().list()]
    runtimes = {a["id"]: a["runtime"] for a in agents}
    assert "no_execution_binding" in runtimes.values() and "bound" in runtimes.values()
    assert all(a["persistent_memory"] == "not_connected" for a in agents)


def test_charlie_is_connected_and_other_agents_get_no_charlie_data():
    tracked_state()
    charlie = client.get("/api/memory-explorer/agents/charlie/working-memory", headers=auth(CONTENT)).json()
    assert charlie["runs"]
    for agent in default_registry().list():
        if agent.id == "charlie":
            continue
        body = client.get(f"/api/memory-explorer/agents/{agent.id}/working-memory", headers=auth(CONTENT))
        assert body.json()["state"] == "not_connected" and body.json()["runs"] == []
        assert SECRET_QUESTION not in body.text


# ---- working-memory projection ------------------------------------------------------------------

def test_idle_state_is_explicit():
    body = client.get("/api/memory-explorer/agents/charlie/working-memory", headers=auth()).json()
    assert body["state"] == "idle" and body["runs"] == []
    assert body["schema_version"] == working_memory.SCHEMA


def test_metadata_token_never_receives_content():
    tracked_state()
    r = client.get("/api/memory-explorer/agents/charlie/working-memory", headers=auth(META))
    assert SECRET_QUESTION not in r.text
    run = r.json()["runs"][0]
    assert run["content"] is None and all(s["summary"] is None for s in run["steps"])
    assert not r.json()["content_included"]


def test_content_token_receives_content_and_metadata_matches_state():
    state = tracked_state()
    r = client.get("/api/memory-explorer/agents/charlie/working-memory", headers=auth(CONTENT))
    run = r.json()["runs"][0]
    assert run["content"]["original_question"] == SECRET_QUESTION
    assert run["request_id"] == state.request_id and run["lifecycle"] == "active"
    assert run["decision_count"] == 3 and run["iteration"] == 2
    assert [s["capability"] for s in run["steps"]] == ["research", "security_assessment"]
    assert run["decisions"][1]["reason_code"] == "missing_dependency"
    assert run["budget"]["remaining_decisions"] == state.execution_budget.max_decisions - 3
    assert r.headers["cache-control"] == "no-store"


def test_projection_is_an_allowlist_without_prompts_or_envelopes():
    tracked_state()
    text = client.get("/api/memory-explorer/agents/charlie/working-memory", headers=auth(CONTENT)).text
    for forbidden in ("system_prompt", "envelope", "model_alias", "http://", "Authorization"):
        assert forbidden not in text


def test_completed_run_and_superseded_evidence():
    first, second = make_step(C.RESEARCH), make_step(C.RESEARCH, version=2)
    state = with_steps(first, second)
    state.status, state.completion_kind, state.completion_reason = "completed", "charlie_approved", "charlie_finished"
    state.elapsed_ms = 1234.0
    working_memory.registry.track(state)
    run = client.get("/api/memory-explorer/agents/charlie/working-memory", headers=auth()).json()["runs"][0]
    assert run["lifecycle"] == "completed" and run["completion_kind"] == "charlie_approved"
    assert [s["current"] for s in run["steps"]] == [False, True]


def test_overdue_running_state_is_released():
    state = new_state(started_at=0.0)
    view = working_memory.project_run(state, content=False, clock=lambda: 10_000.0)
    assert view.lifecycle == "released"


def test_tracking_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("MEMORY_EXPLORER_ENABLED", "false")
    working_memory.registry.track(new_state())
    monkeypatch.setenv("MEMORY_EXPLORER_ENABLED", "true")
    assert working_memory.registry.view(content=False).runs == ()


def test_registry_is_bounded():
    for i in range(working_memory.MAX_FINISHED + 10):
        state = new_state(request_id=f"{i:08d}-0000-0000-0000-000000000000")
        state.status = "completed"
        working_memory.registry.track(state)
    assert len(working_memory.registry.view(content=False).runs) == working_memory.MAX_FINISHED


def test_concurrent_reads_and_writes_do_not_break_projection():
    state = tracked_state()
    errors: list[Exception] = []

    def writer():
        for _ in range(200):
            state.add_gap(f"gap-{_}")
            state.completed_steps.append(make_step(C.RESEARCH))
            del state.completed_steps[2:]

    def reader():
        try:
            for _ in range(200):
                working_memory.registry.view(content=True)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=f) for f in (writer, reader, reader)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors


def test_untrusted_text_is_returned_as_data_and_page_never_uses_innerhtml():
    state = tracked_state()
    state.original_question = "<script>alert(1)</script><img src=x onerror=alert(1)>"
    body = client.get("/api/memory-explorer/agents/charlie/working-memory", headers=auth(CONTENT)).json()
    assert body["runs"][0]["content"]["original_question"].startswith("<script>")
    page = (STATIC / "memory-explorer.html").read_text(encoding="utf-8")
    assert "innerHTML" not in page and "outerHTML" not in page and "document.write" not in page
    assert "insertAdjacentHTML" not in page and "eval(" not in page


# ---- persistent memory and events: honest unavailable ---------------------------------------------

def test_persistent_memory_and_events_are_not_connected_and_invent_nothing():
    p = client.get("/api/memory-explorer/agents/charlie/persistent-memory", headers=auth()).json()
    assert p["state"] == "not_connected" and p["records"] == []
    assert p["namespace"] == "/memory/agent/charlie/records"
    e = client.get("/api/memory-explorer/agents/charlie/events", headers=auth()).json()
    assert e["state"] == "not_connected" and e["events"] == []
    sections = client.get("/api/memory-explorer/agents/charlie", headers=auth()).json()["sections"]
    states = {s["key"]: s["state"] for s in sections}
    assert states["working"] == "connected" and states["persistent"] == "not_connected"
    assert states["episodic"] == states["relationship"] == "not_implemented"


# ---- Command Central and URLs ------------------------------------------------------------------

def test_command_central_is_planned_without_configuration(monkeypatch):
    cfg = client.get("/api/memory-explorer/config", headers=auth()).json()
    assert cfg["command_central"] == {"state": "planned", "url": None, "label": "Command Central"}
    monkeypatch.setenv("COMMAND_CENTRAL_URL", "https://founder.example.com")
    cfg = client.get("/api/memory-explorer/config", headers=auth()).json()
    assert cfg["command_central"]["state"] == "available" and cfg["environment"] == "local"


@pytest.mark.parametrize("bad", ["http://evil.example.com", "https://user:pw@x.com", "javascript:alert(1)",
                                 "https://x.com/?q=1"])
def test_unsafe_urls_are_refused(monkeypatch, bad):
    monkeypatch.setenv("COMMAND_CENTRAL_URL", bad)
    with pytest.raises(RuntimeError):
        client.get("/api/memory-explorer/config", headers=auth())


# ---- website entry point ---------------------------------------------------------------------------

def test_charlie_portrait_links_to_the_explorer_with_accessible_label(monkeypatch):
    html = render_profiles(team_profiles())
    assert ('<a class="explorer-portrait-link" href="/agents/charlie" '
            'aria-label="Explore Charlie&#x27;s Memory"><img class="profile-portrait" '
            'src="/images/Charlie_Chatwin_headshot.png"') in html
    assert "Explore Charlie&#x27;s Memory <span" in html
    assert html.count("explorer-portrait-link") == 1  # only Charlie in phase 1
    monkeypatch.setenv("MEMORY_EXPLORER_BASE_URL", "https://agent-memory.example.com")
    assert 'href="https://agent-memory.example.com/agents/charlie"' in render_profiles(team_profiles())


def test_anonymous_visitors_get_the_link_but_no_memory():
    tracked_state()
    home = client.get("/").text
    assert 'href="/agents/charlie"' in home
    assert SECRET_QUESTION not in home
    assert client.get("/api/memory-explorer/agents/charlie/working-memory").status_code == 401


def test_portrait_asset_is_on_the_allowlist():
    assert "Charlie_Chatwin_headshot.png" in web_app.TEAM_IMAGES


def test_page_has_accessible_controls_and_clears_state_on_switch():
    page = (STATIC / "memory-explorer.html").read_text(encoding="utf-8")
    assert re.search(r'<label for="agent-select">', page) and re.search(r'<label for="token">', page)
    assert 'aria-live="polite"' in page and '<html lang="en">' in page
    assert re.search(r'function load\(agentId\) \{\s*var mine = \+\+seq;\s*resetPanels', page)
    assert "localStorage" not in page and "sessionStorage" not in page


def test_no_model_inference_is_imported_by_the_explorer():
    for module in ("memory_explorer.py", "working_memory.py", "explorer_links.py"):
        source = (Path(web_app.__file__).with_name(module)).read_text(encoding="utf-8")
        assert "litellm" not in source.lower() and "model_agent" not in source


def test_real_pipeline_run_feeds_the_projection_and_disabled_run_retains_nothing(monkeypatch):
    from test_iterative_execution import FULL, QUESTION, run, sequence

    result, _ = run(sequence(*FULL))
    runs = working_memory.registry.view(content=True).runs
    assert len(runs) == 1 and runs[0].lifecycle == "completed"
    assert runs[0].request_id == result.orchestration.request_id
    assert [s.capability for s in runs[0].steps] == [c.value for c in FULL]
    assert runs[0].content.original_question.startswith(QUESTION[:20])

    working_memory.registry.clear()
    monkeypatch.setenv("MEMORY_EXPLORER_ENABLED", "false")
    run(sequence(*FULL))
    monkeypatch.setenv("MEMORY_EXPLORER_ENABLED", "true")
    assert working_memory.registry.view(content=False).runs == ()
