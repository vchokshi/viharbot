"""Runs the page's own script in Node against a fake DOM (see tests/js). Skipped without Node."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline_fixtures import FINDINGS, RESPONSES

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "src" / "viharbot" / "static" / "index.html"
HARNESS = Path(__file__).resolve().parent / "js" / "run_page.mjs"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

ALLOWED_TAGS = {"div", "span", "p", "strong", "ul", "ol", "li", "section", "article", "details",
                "summary", "h4", "time", "code", "i", "svg", "path", "circle", "small"}
HOSTILE = '<img src=x onerror="alert(1)"><script>alert(2)</script>'


def activity(agent="research", display="Research", alias="m-r", ms=1200):
    return {"agent": agent, "display_name": display, "model_alias": alias, "duration_ms": ms,
            "status": "complete"}


def chat_body(**overrides):
    body = {
        "request_id": "req-1", "findings": FINDINGS,
        "assessment": RESPONSES["security"]["assessment"], "fact_check": None,
        "source_quality": None, "privacy_review": None, "synthesis": None,
        "agent_activity": [activity(), activity("security", "Security", "m-sec", 800)],
    }
    return {**body, **overrides}


def meta(**overrides):
    base = {
        "mode": "iterative", "status": "completed", "completion_kind": "charlie_approved",
        "completion_reason": "charlie_finished", "decisions": 3, "executions": 2,
        "rejected_actions": 0, "elapsed_ms": 2100.0,
        "steps": [
            {"sequence": 1, "identity": "nova", "capability": "research", "service": "research",
             "version": 1, "status": "complete", "duration_ms": 1200.0, "in_final_result": True},
            {"sequence": 2, "identity": "cipher", "capability": "security_assessment",
             "service": "security", "version": 1, "status": "complete", "duration_ms": 800.0,
             "in_final_result": True},
        ],
    }
    return {**base, **overrides}


def run_page(scenario: dict) -> dict:
    completed = subprocess.run([NODE, str(HARNESS), str(INDEX)], input=json.dumps(scenario),
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def respond(body, status=200, message="hello"):
    return run_page({"message": message, "response": {"status": status, "body": body}})


def walk(node):
    yield node
    for child in node.get("children", []):
        yield from walk(child)


def elements(tree):
    return [n for n in walk(tree) if "tag" in n]


def with_class(tree, name):
    return [n for n in elements(tree) if name in n["class"].split()]


def text_of(node):
    if "text" in node:
        return node["text"]
    return "".join(text_of(c) for c in node["children"])


def all_text(tree):
    return text_of(tree)


# 12. responses without metadata render exactly as before
@needs_node
def test_a_response_without_metadata_renders_the_existing_view():
    tree = respond(chat_body())["final"]["tree"]
    assert with_class(tree, "response-card") and with_class(tree, "activity-panel")
    assert not with_class(tree, "orchestration-panel") and not with_class(tree, "answer-source")
    text = all_text(tree)
    assert "A considered answer" in text and FINDINGS["summary"] in text
    assert "The team at work" in text and "2 calls" in text


@needs_node
def test_optional_reviews_stay_optional_and_appear_only_when_returned():
    bare = all_text(respond(chat_body())["final"]["tree"])
    for heading in ("Fact Check", "Source Quality", "Privacy review"):
        assert heading not in bare
    full = all_text(respond(chat_body(
        fact_check=RESPONSES["fact-check"]["result"],
        source_quality=RESPONSES["source-quality"]["result"],
        privacy_review=RESPONSES["privacy-review"]["result"],
        synthesis=RESPONSES["synthesis"]["result"]))["final"]["tree"])
    assert "Fact Check · supplied evidence only" in full and "Source Quality" in full
    assert "Privacy review" in full and RESPONSES["synthesis"]["result"]["answer"] in full


# iterative: coordination panel
@needs_node
def test_iterative_responses_show_charlies_coordination_and_the_actual_steps():
    tree = respond(chat_body(orchestration=meta()))["final"]["tree"]
    panel = with_class(tree, "orchestration-panel")[0]
    assert panel["attrs"]["aria-label"] == "How Charlie coordinated this request"
    text = text_of(panel)
    assert "3 decisions · 2 delegated runs" in text and "2.1 s" in text
    rows = with_class(panel, "step-row")
    assert [text_of(r) for r in rows] == [
        "01Nova · Research1.2 sDone", "02Cipher · Security800 msDone"]
    assert all(r["tag"] == "li" for r in rows)
    assert with_class(panel, "orchestration-steps")[0]["tag"] == "ol"  # ordered, screen-reader friendly
    assert with_class(tree, "activity-panel")  # the existing model and timing report remains


@needs_node
def test_repeated_and_superseded_executions_are_visible():
    steps = meta()["steps"] + [
        {"sequence": 3, "identity": "nova", "capability": "research", "service": "research",
         "version": 2, "status": "complete", "duration_ms": 900.0, "in_final_result": True}]
    steps[0]["in_final_result"] = False
    panel = with_class(respond(chat_body(orchestration=meta(steps=steps, executions=3)))
                       ["final"]["tree"], "orchestration-panel")[0]
    rows = with_class(panel, "step-row")
    assert len(rows) == 3 and "Not in the final answer" in text_of(rows[0])
    assert "Run 2" in text_of(rows[2]) and "Run" not in text_of(rows[1])
    assert "superseded" in rows[0]["class"] and "superseded" not in rows[2]["class"]


# 4, 5. Charlie versus policy completion
@needs_node
def test_charlie_and_policy_completion_are_visually_and_textually_distinct():
    charlie = with_class(respond(chat_body(orchestration=meta()))["final"]["tree"], "completion-badge")[0]
    assert text_of(charlie) == "Finished by Charlie" and "charlie" in charlie["class"].split()
    policy_meta = meta(completion_kind="policy_budget", completion_reason="execution_budget",
                       status="budget_exhausted")
    tree = respond(chat_body(orchestration=policy_meta))["final"]["tree"]
    policy = with_class(tree, "completion-badge")[0]
    assert text_of(policy) == "Completed under policy budget" and "policy" in policy["class"].split()
    note = text_of(with_class(tree, "completion-note")[0])
    assert "Charlie did not finish" in note and "application completed" in note


@needs_node
@pytest.mark.parametrize("reason,phrase", [
    ("charlie_finished", "decided it was sufficient"),
    ("decision_budget", "decision limit"),
    ("execution_budget", "limit on delegated runs"),
    ("time_budget", "time limit"),
    ("rejection_limit", "refused"),
])
def test_every_completion_reason_has_its_own_plain_explanation(reason, phrase):
    kind = "charlie_approved" if reason == "charlie_finished" else "policy_budget"
    tree = respond(chat_body(orchestration=meta(completion_kind=kind, completion_reason=reason)))["final"]["tree"]
    assert phrase in text_of(with_class(tree, "completion-note")[0])


# answer attribution
@needs_node
def test_the_answer_source_is_stated_and_never_credited_to_charlie():
    with_synthesis = respond(chat_body(synthesis=RESPONSES["synthesis"]["result"],
                                       orchestration=meta()))["final"]["tree"]
    assert text_of(with_class(with_synthesis, "answer-source")[0]) == "From the Synthesis agent."
    without = respond(chat_body(orchestration=meta()))["final"]["tree"]
    source = text_of(with_class(without, "answer-source")[0])
    assert "research summary" in source and "No synthesis was run" in source
    assert "Charlie" not in source and "Charlie" not in text_of(with_class(with_synthesis, "answer-source")[0])


@needs_node
def test_the_ui_does_not_claim_that_all_six_agents_ran():
    tree = respond(chat_body(orchestration=meta()))["final"]["tree"]
    text = all_text(tree).lower()
    assert "six" not in text and "all six" not in text
    assert len(with_class(tree, "step-row")) == 2


# 13. untrusted text is rendered as text
@needs_node
def test_hostile_model_output_becomes_text_never_markup():
    hostile = {**FINDINGS, "summary": HOSTILE,
               "findings": [{"claim": HOSTILE, "evidence": HOSTILE, "confidence": "high"}],
               "sources": [HOSTILE], "uncertainties": [HOSTILE]}
    body = chat_body(
        findings=hostile,
        synthesis={"answer": HOSTILE, "key_points": [HOSTILE], "confidence": "high",
                   "caveats": [HOSTILE]},
        orchestration=meta(steps=[{**meta()["steps"][0], "identity": HOSTILE, "capability": HOSTILE}],
                           completion_reason=HOSTILE),
    )
    result = respond(body, message=HOSTILE)
    for snapshot in (result["pending"]["tree"], result["final"]["tree"]):
        for node in elements(snapshot):
            assert node["tag"] in ALLOWED_TAGS, node["tag"]
            assert not any(name.lower().startswith("on") for name in node["attrs"])
    assert result["intervals"]["cleared"] == 1
    assert HOSTILE in all_text(result["final"]["tree"])  # present, but only as text
    assert "img" not in result["createdTags"] and "script" not in result["createdTags"]
    assert result["fetchCalls"][0]["body"]["message"] == HOSTILE  # sent as data, unchanged


@needs_node
def test_hostile_text_in_errors_and_activity_is_text_too():
    result = respond({"detail": {"message": HOSTILE, "agent_activity": [activity(display=HOSTILE)]}}, status=502)
    assert all(n["tag"] in ALLOWED_TAGS for n in elements(result["final"]["tree"]))
    assert HOSTILE in all_text(result["final"]["tree"])


def test_the_page_never_assigns_markup_strings():
    html = INDEX.read_text()
    assert not re.search(r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write", html)
    script = re.search(r"<script>\n(.*?)</script>", html, re.S).group(1)
    assert "innerHTML" not in script and "eval(" not in script and "new Function" not in script


# 14. loading, success and error states
@needs_node
def test_the_loading_state_is_neutral_accessible_and_replaced_by_the_result():
    result = respond(chat_body(orchestration=meta()))
    pending = with_class(result["pending"]["tree"], "pending-card")[0]
    assert pending["attrs"]["role"] == "status" and pending["attrs"]["aria-live"] == "polite"
    text = text_of(pending)
    assert "The team is working on your question" in text and "In progress" in text
    assert "six" not in text.lower() and "Six" not in text
    bar = with_class(pending, "progress-track")[0]
    assert bar["attrs"]["role"] == "progressbar" and "six" not in json.dumps(bar["attrs"]).lower()
    assert result["pending"]["sendDisabled"] is True and result["final"]["sendDisabled"] is False
    assert not with_class(result["final"]["tree"], "pending-card")
    assert result["intervals"] == {"set": 1, "cleared": 1}


@needs_node
def test_the_request_sent_to_the_api_is_unchanged():
    call = respond(chat_body())["fetchCalls"][0]
    assert call["url"] == "/api/chat" and call["method"] == "POST"
    assert set(call["body"]) == {"message", "history"}


@needs_node
@pytest.mark.parametrize("body,status,expected", [
    ({"detail": "The agent team could not complete this request"}, 502,
     "The agent team could not complete this request"),
    ({"detail": {"message": "The agent team could not complete this request",
                 "agent_activity": [activity("security", "Security", None, 50) | {"status": "failed"}]}},
     502, "The agent team could not complete this request"),
])
def test_error_responses_show_an_error_card_without_orchestration_details(body, status, expected):
    result = respond(body, status=status)
    tree = result["final"]["tree"]
    assert expected in text_of(with_class(tree, "error-card")[0])
    assert not with_class(tree, "orchestration-panel") and not with_class(tree, "pending-card")
    assert result["final"]["sendDisabled"] is False
    if isinstance(body["detail"], dict):
        assert "Failed" in all_text(tree)


@needs_node
def test_a_network_failure_shows_an_error_and_unblocks_the_form():
    result = run_page({"message": "hi", "reject": "Failed to fetch"})
    tree = result["final"]["tree"]
    assert "Failed to fetch" in text_of(with_class(tree, "error-card")[0])
    assert result["final"]["sendDisabled"] is False and result["intervals"]["cleared"] == 1


@needs_node
def test_malformed_orchestration_metadata_does_not_break_the_page():
    tree = respond(chat_body(orchestration={"mode": "iterative", "completion_kind": "mystery"}))["final"]["tree"]
    assert with_class(tree, "response-card") and with_class(tree, "orchestration-panel")
    assert not with_class(tree, "error-card")
    assert text_of(with_class(tree, "completion-badge")[0]) == "Finished by Charlie"  # the safe default
    unknown = respond(chat_body(orchestration=meta(completion_reason="made_up")))["final"]["tree"]
    assert "made_up" in text_of(with_class(unknown, "completion-note")[0])


# no new infrastructure, accessibility and layout (static checks that need no browser)
def test_the_page_adds_no_frameworks_streaming_or_persistent_storage():
    html = INDEX.read_text()
    assert "<script src" not in html and "<link rel=\"stylesheet\"" not in html
    script = re.search(r"<script>\n(.*?)</script>", html, re.S).group(1)
    for banned in ("WebSocket", "EventSource", "localStorage", "sessionStorage", "indexedDB",
                   "getReader", "ReadableStream", "import(", "require("):
        assert banned not in script, banned
    assert script.count("setInterval") == 1  # only the existing elapsed-time label


def test_the_new_panel_is_responsive_and_keyboard_friendly_without_a_new_style_system():
    html = INDEX.read_text()
    css = re.search(r'<style id="orchestration-styles">(.*?)</style>', html, re.S).group(1)
    assert "@media (max-width: 680px)" in css and ".step-row" in css
    assert "outline: none" not in css and "outline:none" not in css
    assert "var(--green-soft)" in css  # the existing design tokens are reused


def test_the_page_never_describes_a_live_feed():
    script = re.search(r"<script>\n(.*?)</script>", INDEX.read_text(), re.S).group(1)
    assert "not a live feed" in script
    claims = re.findall(r"(?i)\b(live (?:feed|progress|updates?|activity)|real-?time|streaming)\b",
                        script.replace("not a live feed", ""))
    assert claims == []
