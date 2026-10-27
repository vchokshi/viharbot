from __future__ import annotations

import importlib

import pytest
from pydantic import ValidationError

from pipeline_fixtures import ORDER, RID, URLS, FakeSend, response
from viharbot.contracts import (
    ResponseContractError,
    SpecialistResponse,
    validate_agent_response,
)
from viharbot.demo import AgentPipelineError, run_pipeline
from viharbot import research_agent, security_agent


def drop(key):
    return lambda r: {k: v for k, v in r.items() if k != key}


def setting(key, value):
    return lambda r: {**r, key: value}


def nested(path, value):
    def edit(r):
        node = r
        for part in path[:-1]:
            node = node[part][0] if isinstance(node[part], list) else node[part]
        node[path[-1]] = value
        return r

    return edit


BAD_ENVELOPES = [
    ("wrong_request_id", setting("request_id", "other")),
    ("wrong_agent", setting("agent", "somebody-else")),
    ("missing_agent", drop("agent")),
    ("not_an_object", lambda r: ["not", "an", "object"]),
]
BAD_DOMAIN = {
    "research": [
        ("missing_findings", drop("findings")),
        ("bad_confidence", nested(["findings", "findings", "confidence"], "certain")),
        ("malformed_finding", nested(["findings", "findings"], [{"claim": "only"}])),
    ],
    "security": [
        ("missing_assessment", drop("assessment")),
        ("bad_risk", nested(["assessment", "overall_risk"], "critical")),
        ("missing_field", nested(["assessment", "recommendations"], None)),
    ],
    "fact-check": [
        ("missing_result", drop("result")),
        ("bad_enum", nested(["result", "claims", "status"], "verified")),
        ("missing_field", nested(["result", "limitations"], None)),
    ],
    "source-quality": [
        ("missing_result", drop("result")),
        ("bad_enum", nested(["result", "sources", "apparent_quality"], "great")),
        ("missing_field", nested(["result", "limitations"], None)),
    ],
    "privacy-review": [
        ("missing_result", drop("result")),
        ("bad_enum", nested(["result", "overall_risk"], "severe")),
        ("missing_field", nested(["result", "processing_note"], None)),
    ],
    "synthesis": [
        ("missing_result", drop("result")),
        ("bad_enum", nested(["result", "confidence"], "certain")),
        ("missing_field", nested(["result", "answer"], None)),
    ],
}
CASES = [
    (agent, name, edit)
    for agent in ORDER
    for name, edit in BAD_ENVELOPES + BAD_DOMAIN[agent]
]


@pytest.mark.parametrize("destination", ORDER)
def test_valid_response_is_accepted_with_typed_domain(destination):
    envelope, domain = validate_agent_response(destination, response(destination), RID)
    assert envelope.request_id == RID
    assert type(domain).__name__ in {
        "ResearchFindings", "SecurityAssessment", "FactCheckReview",
        "SourceQualityReview", "PrivacyAssessment", "FinalSynthesis",
    }


@pytest.mark.parametrize("destination,case,edit", CASES, ids=lambda v: v if isinstance(v, str) else "")
def test_malformed_response_is_rejected(destination, case, edit):
    raw = edit(response(destination))
    with pytest.raises((ValidationError, ResponseContractError)):
        validate_agent_response(destination, raw, RID)


@pytest.mark.parametrize("destination,case,edit", CASES, ids=lambda v: v if isinstance(v, str) else "")
def test_malformed_response_fails_pipeline_immediately(destination, case, edit):
    send = FakeSend({destination: edit})
    with pytest.raises(AgentPipelineError) as caught:
        run_pipeline("q", RID, research_url="u-r", security_url="u-sec",
                     specialist_urls=URLS, send=send)
    activity = caught.value.activity
    assert activity[-1]["agent"] == destination
    assert activity[-1]["status"] == "failed"
    assert activity[-1]["error"] in {"ValidationError", "ResponseContractError"}
    assert all(a["status"] == "complete" for a in activity[:-1])
    # Nothing after the failed stage ran.
    assert send.order == ORDER[: ORDER.index(destination) + 1]


def test_error_text_does_not_echo_response_content():
    raw = response("research")
    raw["request_id"] = "SECRET-VALUE"
    with pytest.raises(ResponseContractError) as caught:
        validate_agent_response("research", raw, RID)
    assert "SECRET-VALUE" not in str(caught.value)


def test_envelope_models_are_still_exported_from_agent_modules(monkeypatch):
    monkeypatch.setenv("AGENT_KIND", "synthesis")
    specialist_agent = importlib.import_module("viharbot.specialist_agent")
    assert research_agent.ResearchResponse.__module__ == "viharbot.contracts"
    assert security_agent.SecurityResponse.__module__ == "viharbot.contracts"
    assert specialist_agent.SpecialistResponse is SpecialistResponse
