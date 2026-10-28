"""The evaluation set and its scoring. Offline: no model is ever called here."""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from viharbot.schemas import ResearchFindings

PATH = Path(__file__).resolve().parent.parent / "scripts" / "fact_check_eval" / "run.py"
spec = importlib.util.spec_from_file_location("fact_check_eval_run", PATH)
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)
CASES = ev.load_cases()
S, C, M, I = ev.STATUSES


def test_every_case_is_valid_research_and_labelled_per_claim():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)) and len(CASES) >= 30
    for case in CASES:
        findings = ResearchFindings.model_validate(case["findings"])
        assert len(case["expected"]) == len(findings.findings) == len(case["also_acceptable"])
        assert set(case["expected"]) <= set(ev.STATUSES)
        assert all(set(extra) <= set(ev.STATUSES) for extra in case["also_acceptable"])
        assert case["question"].strip() and case["category"]


def test_the_set_covers_every_verdict_and_the_hard_cases():
    counts = Counter(x for case in CASES for x in case["expected"])
    assert all(counts[s] >= 5 for s in ev.STATUSES)
    categories = {c["category"] for c in CASES}
    assert {"supported", "contradicted", "mixed", "insufficient", "injection", "multi"} <= categories
    injected = [c for c in CASES if c["category"] == "injection"]
    assert len(injected) >= 3 and any("ignore" in str(c).lower() for c in injected)


def test_a_perfect_reviewer_scores_perfectly():
    results = ev.evaluate(CASES, lambda case: [{"status": e, "rationale": "r"} for e in case["expected"]])
    summary = ev.summarize(results)
    assert summary["exact"] == summary["acceptable"] == 1 and summary["false_support"] == 0
    assert summary["errors"] == 0 and summary["wrong"] == []


def test_an_always_supported_reviewer_is_caught_as_unsafe():
    results = ev.evaluate(CASES, lambda case: [{"status": S, "rationale": "r"} for _ in case["expected"]])
    summary = ev.summarize(results)
    assert summary["false_support"] > 20 and summary["acceptable"] < 0.35
    assert summary["injection_pass"] is not None and summary["injection_pass"] < 0.5


def test_an_alternate_verdict_counts_as_acceptable_but_not_exact():
    case = next(c for c in CASES if c["id"] == "mix-age")
    row = ev.judge(case, [C])[0]
    assert row["acceptable"] and not row["exact"] and not row["false_support"]


def test_a_failed_call_is_a_wrong_answer_not_a_crash():
    def boom(case):
        raise RuntimeError("502")
    summary = ev.summarize(ev.evaluate(CASES[:3], boom))
    assert summary["errors"] == 3 and summary["acceptable"] == 0 and len(summary["wrong"]) == 3


def test_downgraded_verdicts_are_counted():
    from viharbot.fact_check_guard import DOWNGRADE_NOTE
    results = ev.evaluate(CASES[:1], lambda case: [{"status": I, "rationale": "r" + DOWNGRADE_NOTE}])
    assert ev.summarize(results)["downgraded_verdicts"] == 1


def test_the_live_run_refuses_without_explicit_billing_confirmation(capsys):
    assert ev.main(["--model", "x/y", "--limit", "2"]) == 2
    assert "billable" in capsys.readouterr().err


def test_the_report_names_the_model_and_the_mistakes():
    summary = ev.summarize(ev.evaluate(CASES[:2], lambda case: [{"status": I, "rationale": "r"}]))
    text = ev.report("some/model", summary)
    assert "some/model" in text and "Exact match" in text and "Wrong:" in text


def test_the_live_run_stops_early_when_litellm_settings_are_missing(monkeypatch, capsys):
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    monkeypatch.setenv("LITELLM_API_KEY", "set")
    assert ev.main(["--model", "x/y", "--confirm-billable"]) == 2
    assert "LITELLM_BASE_URL" in capsys.readouterr().err
