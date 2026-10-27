"""Tiny orchestrator that delegates over private JSON REST endpoints."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import BaseModel

from .contracts import AgentResponse, validate_agent_response
from .observability import configure_logging
from .pipeline_state import (
    AgentActivity,
    PipelineContext,
    PipelineState,
    legacy_context_payload,
    legacy_findings_payload,
)

configure_logging()
logger = logging.getLogger("orchestrator")

AGENT_LABELS = {
    "research": "Research",
    "fact-check": "Fact Check",
    "source-quality": "Source Quality",
    "security": "Security",
    "privacy-review": "Privacy Review",
    "synthesis": "Synthesis",
}


class AgentPipelineError(RuntimeError):
    def __init__(self, activity: list[dict]) -> None:
        super().__init__("an agent call failed")
        self.activity = activity


def post_json(url: str, payload: dict, request_id: str, destination: str) -> dict:
    started = time.perf_counter()
    if url.startswith("agentcore://"):
        from .agentcore_client import invoke_agentcore

        try:
            result = invoke_agentcore(url.removeprefix("agentcore://"), payload, request_id)
        except Exception as exc:
            logger.error(
                "agent call failed",
                extra={
                    "agent": "orchestrator",
                    "request_id": request_id,
                    "destination_agent": destination,
                    "model_alias": None,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "error": type(exc).__name__,
                },
            )
            raise
        logger.info(
            "agent call completed",
            extra={
                "agent": "orchestrator",
                "request_id": request_id,
                "destination_agent": destination,
                "model_alias": None,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return result

    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Request-ID": request_id},
    )
    try:
        with urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.error(
            "agent call failed",
            extra={
                "agent": "orchestrator",
                "request_id": request_id,
                "destination_agent": destination,
                "model_alias": None,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "error": type(exc).__name__,
            },
        )
        raise
    logger.info(
        "agent call completed",
        extra={
            "agent": "orchestrator",
            "request_id": request_id,
            "destination_agent": destination,
            "model_alias": None,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return result


def run_pipeline(
    question: str,
    request_id: str,
    *,
    research_url: str,
    security_url: str,
    specialist_urls: dict[str, str] | None = None,
    send: Callable[[str, dict, str, str], dict] = post_json,
) -> dict:
    return run_typed_pipeline(
        question,
        request_id,
        research_url=research_url,
        security_url=security_url,
        specialist_urls=specialist_urls,
        send=send,
    ).to_legacy_dict()


def invoke_agent(
    send: Callable[[str, dict, str, str], dict],
    url: str,
    payload: dict,
    request_id: str,
    destination: str,
    activity: list[AgentActivity],
) -> tuple[AgentResponse, BaseModel]:
    """Call one service, validate its response and record the activity.

    This is the smallest unit of pipeline execution: one capability, one validated response.
    """
    started = time.perf_counter()
    try:
        envelope, domain = validate_agent_response(
            destination, send(url, payload, request_id, destination), request_id
        )
    except Exception as exc:
        activity.append(
            AgentActivity(
                agent=destination,
                display_name=AGENT_LABELS.get(destination, destination),
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                status="failed",
                error=type(exc).__name__,
            )
        )
        raise AgentPipelineError([item.to_legacy() for item in activity]) from None
    activity.append(
        AgentActivity(
            agent=destination,
            display_name=AGENT_LABELS.get(destination, destination),
            model_alias=envelope.requested_model_alias,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            status="complete",
        )
    )
    return envelope, domain


def run_typed_pipeline(
    question: str,
    request_id: str,
    *,
    research_url: str,
    security_url: str,
    specialist_urls: dict[str, str] | None = None,
    send: Callable[[str, dict, str, str], dict] = post_json,
) -> PipelineState:
    activity: list[AgentActivity] = []

    def invoke(
        url: str, payload: dict, destination: str
    ) -> tuple[AgentResponse, BaseModel]:
        return invoke_agent(send, url, payload, request_id, destination, activity)

    research, findings = invoke(research_url, {"question": question}, "research")
    responses: dict[str, AgentResponse] = {"research": research}
    context = PipelineContext(research=findings)

    for name in ("fact_check", "source_quality"):
        url = (specialist_urls or {}).get(name)
        if url:
            responses[name], domain = invoke(
                url, legacy_findings_payload(findings), name.replace("_", "-")
            )
            setattr(context, name, domain)

    responses["security"], context.security = invoke(
        security_url, legacy_findings_payload(findings), "security"
    )

    legacy_context = legacy_context_payload(responses)
    privacy_url = (specialist_urls or {}).get("privacy_review")
    if privacy_url:
        responses["privacy_review"], context.privacy_review = invoke(
            privacy_url,
            {"question": question, "context": legacy_context},
            "privacy-review",
        )

    synthesis = None
    synthesis_url = (specialist_urls or {}).get("synthesis")
    if synthesis_url:
        privacy_response = responses.get("privacy_review")
        responses["synthesis"], synthesis = invoke(
            synthesis_url,
            {
                "question": question,
                "context": {
                    **legacy_context,
                    "privacy_review": privacy_response.result if privacy_response else None,
                },
            },
            "synthesis",
        )

    return PipelineState(
        request_id=request_id,
        context=context,
        synthesis=synthesis,
        responses=responses,
        agent_activity=activity,
    )


def main() -> None:
    question = os.getenv(
        "RESEARCH_QUESTION", "Summarize general safety practices for bicycle maintenance."
    )
    result = run_pipeline(
        question,
        str(uuid4()),
        research_url=os.getenv("RESEARCH_URL", "http://research:8000/research"),
        security_url=os.getenv("SECURITY_URL", "http://security:8000/assess"),
        specialist_urls={
            "fact_check": os.getenv(
                "FACT_CHECK_URL", "http://fact-check:8000/process"
            ),
            "source_quality": os.getenv(
                "SOURCE_QUALITY_URL", "http://source-quality:8000/process"
            ),
            "privacy_review": os.getenv(
                "PRIVACY_REVIEW_URL", "http://privacy-review:8000/process"
            ),
            "synthesis": os.getenv("SYNTHESIS_URL", "http://synthesis:8000/process"),
        },
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
