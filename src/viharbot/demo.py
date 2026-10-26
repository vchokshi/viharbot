"""Tiny orchestrator that delegates over private JSON REST endpoints."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable
from urllib.request import Request, urlopen
from uuid import uuid4

from .observability import configure_logging

configure_logging()
logger = logging.getLogger("orchestrator")


def post_json(url: str, payload: dict, request_id: str, destination: str) -> dict:
    started = time.perf_counter()
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
    research = send(research_url, {"question": question}, request_id, "research")
    results: dict[str, dict] = {"research": research}

    specialist_inputs = {
        "fact_check": {"findings": research["findings"]},
        "source_quality": {"findings": research["findings"]},
    }
    for name in ("fact_check", "source_quality"):
        url = (specialist_urls or {}).get(name)
        if url:
            results[name] = send(url, specialist_inputs[name], request_id, name)

    security = send(
        security_url,
        {"findings": research["findings"]},
        request_id,
        "security",
    )
    results["security"] = security

    context = {"research": research["findings"]}
    context.update(
        {
            name: result.get("result", result)
            for name, result in results.items()
            if name != "research"
        }
    )
    privacy_url = (specialist_urls or {}).get("privacy_review")
    if privacy_url:
        results["privacy_review"] = send(
            privacy_url,
            {"question": question, "context": context},
            request_id,
            "privacy-review",
        )

    synthesis_url = (specialist_urls or {}).get("synthesis")
    if synthesis_url:
        results["synthesis"] = send(
            synthesis_url,
            {
                "question": question,
                "context": {
                    **context,
                    "privacy_review": results.get("privacy_review", {}).get("result"),
                },
            },
            request_id,
            "synthesis",
        )

    return {"request_id": request_id, **results}


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
