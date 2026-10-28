#!/usr/bin/env python3
"""Score the Fact Check agent against a labelled set of claims.

Each case gives claims with their own evidence text and the verdict a careful reviewer should
reach using that text alone. The live run goes through the same code path as the service
(prompt, structured output, citation guard), so it measures what users get.

Running it makes billable LiteLLM calls (one per case), so it refuses unless you pass
--confirm-billable. Scoring and loading are pure and are covered by offline tests.

    python scripts/fact_check_eval/run.py --model gemini/gemini-3.5-flash --confirm-billable
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES_FILE = HERE / "cases.json"
STATUSES = (
    "supported_by_supplied_evidence",
    "contradicted_by_supplied_evidence",
    "mixed_evidence",
    "insufficient_evidence",
)
SUPPORTED = STATUSES[0]
DOWNGRADE_MARKER = "Downgraded: no quote"


def load_cases(path: Path = CASES_FILE) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def judge(case: dict, predicted: list[str | None]) -> list[dict]:
    """Per-claim verdict comparison. A missing or failed prediction counts as wrong."""
    rows = []
    for i, expected in enumerate(case["expected"]):
        got = predicted[i] if i < len(predicted) else None
        allowed = {expected, *case.get("also_acceptable", [[]] * len(case["expected"]))[i]}
        rows.append({
            "claim_index": i, "expected": expected, "got": got,
            "exact": got == expected, "acceptable": got in allowed,
            # The costly mistake: telling a reader a claim is backed when its evidence does not.
            "false_support": got == SUPPORTED and SUPPORTED not in allowed,
        })
    return rows


def summarize(results: list[dict]) -> dict:
    """Aggregate per-case results. Each result: case, ok, rows, downgraded, seconds."""
    rows = [dict(r, category=res["category"], case_id=res["id"]) for res in results for r in res["rows"]]
    total = len(rows) or 1
    confusion = Counter((r["expected"], r["got"] or "error") for r in rows)
    injection = [r for r in rows if r["category"] == "injection"]
    timings = [res["seconds"] for res in results if res.get("seconds") is not None]
    return {
        "cases": len(results), "claims": len(rows),
        "errors": sum(1 for res in results if not res["ok"]),
        "exact": sum(r["exact"] for r in rows) / total,
        "acceptable": sum(r["acceptable"] for r in rows) / total,
        "false_support": sum(r["false_support"] for r in rows),
        "downgraded_verdicts": sum(res.get("downgraded", 0) for res in results),
        "injection_pass": (sum(r["acceptable"] for r in injection) / len(injection)) if injection else None,
        "mean_seconds": sum(timings) / len(timings) if timings else None,
        "confusion": {f"{e} -> {g}": n for (e, g), n in sorted(confusion.items())},
        "wrong": [f"{r['case_id']}[{r['claim_index']}]: expected {r['expected']}, got {r['got']}"
                  for r in rows if not r["acceptable"]],
    }


def report(model: str, summary: dict) -> str:
    pct = lambda x: "n/a" if x is None else f"{100 * x:.0f}%"
    lines = [
        f"Model: {model}",
        f"Cases: {summary['cases']}  Claims: {summary['claims']}  Errors: {summary['errors']}",
        f"Exact match: {pct(summary['exact'])}   Acceptable: {pct(summary['acceptable'])}",
        f"False support (said supported, evidence does not): {summary['false_support']}",
        f"Verdicts downgraded for lacking a confirmed quote: {summary['downgraded_verdicts']}",
        f"Prompt-injection cases passed: {pct(summary['injection_pass'])}",
        f"Mean seconds per case: {'n/a' if summary['mean_seconds'] is None else round(summary['mean_seconds'], 1)}",
    ]
    if summary["wrong"]:
        lines += ["Wrong:"] + [f"  {w}" for w in summary["wrong"]]
    return "\n".join(lines)


def evaluate(cases: list[dict], run_case, workers: int = 1) -> list[dict]:
    """Run every case through run_case(case) -> list of response claims; never raises."""

    def one(case: dict) -> dict:
        started = time.perf_counter()
        try:
            claims = run_case(case)
            predicted = [c["status"] for c in claims]
            downgraded = sum(1 for c in claims if DOWNGRADE_MARKER in c.get("rationale", ""))
            ok = True
        except Exception as exc:  # a failed call is a result, not a crash
            predicted, downgraded, ok = [], 0, False
            claims = [{"error": type(exc).__name__}]
        return {"id": case["id"], "category": case["category"], "ok": ok, "claims": claims,
                "rows": judge(case, predicted), "downgraded": downgraded,
                "seconds": time.perf_counter() - started}

    if workers <= 1:
        return [one(c) for c in cases]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, cases))


def live_runner(model: str):
    """The service's own request handler, pointed at `model`. Imported lazily on purpose."""
    sys.path.insert(0, str(HERE.parents[1] / "src"))
    os.environ["AGENT_KIND"] = "fact-check"
    os.environ["FACT_CHECK_MODEL"] = model
    os.environ["MEMORY_DIR"] = tempfile.mkdtemp(prefix="fact-check-eval-")
    from fastapi import Response

    from viharbot import specialist_agent as service

    def run(case: dict) -> list[dict]:
        body = service.SpecialistRequest(question=case["question"], findings=case["findings"])
        reply = service.process(body, Response(), f"eval-{case['id']}")
        return reply.result["claims"]

    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--model", required=True, help="LiteLLM alias to evaluate")
    parser.add_argument("--cases", type=Path, default=CASES_FILE)
    parser.add_argument("--limit", type=int, help="only the first N cases")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--out", type=Path, help="write per-case results as JSON")
    parser.add_argument("--confirm-billable", action="store_true",
                        help="required: acknowledges one billable model call per case")
    args = parser.parse_args(argv)
    cases = load_cases(args.cases)[: args.limit]
    if not args.confirm_billable:
        print(f"Would make {len(cases)} billable calls to {args.model}. Re-run with --confirm-billable.",
              file=sys.stderr)
        return 2
    missing = [name for name in ("LITELLM_BASE_URL", "LITELLM_API_KEY") if not os.getenv(name, "").strip()]
    if missing:
        print(f"Set {' and '.join(missing)} first (the containers get them from compose).", file=sys.stderr)
        return 2
    results = evaluate(cases, live_runner(args.model), workers=args.workers)
    summary = summarize(results)
    print(report(args.model, summary))
    if summary["errors"] == summary["cases"]:
        print("Every call failed, so these numbers say nothing about the model.", file=sys.stderr)
    if args.out:
        args.out.write_text(json.dumps({"model": args.model, "summary": summary, "results": results},
                                       indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
