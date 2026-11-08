"""Issue #142 (slice 2): deterministic governance comparison. Offline; no model, no network."""

from __future__ import annotations

import ast
import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from test_archie_sage_evidence import OTHER_HEAD, comment, sage_body
from test_learning_pipeline import BODY, SHA, FakeGitHub, reader
from tests.sage_review_helpers import HEAD
from viharbot.learning.extract import extract, render_markdown
from viharbot.learning.governance import FOUNDER_REVIEW_PATHS, RATIONALE_SECTIONS, compare
from viharbot.learning.models import ChangedPath, PRSnapshot, SageEvidence, SageFinding, Verification

ROOT = Path(__file__).resolve().parent.parent
FULL_BODY = "\n\n".join(f"## {h}\ntext" for h in (
    "WHY — Why was this change necessary?", "WHAT changed?", "WHY THIS WAY — Decision rationale",
    "Alternatives considered", "Trade-offs and risks", "Validation and evidence",
    "Deployment, rollback, and follow-up"))


def snap(body=FULL_BODY, paths=("src/viharbot/x.py",), sage=None) -> PRSnapshot:
    return PRSnapshot(
        repository="vchokshi/viharbot", number=9, title="t", body=body,
        merged_at=datetime(2026, 11, 7, tzinfo=timezone.utc), merge_commit_sha=SHA, base_ref="main",
        files=tuple(ChangedPath(path=p, status="modified") for p in paths), sage_review=sage)


def sage(recommendation="APPROVE", severities=(), current=True) -> SageEvidence:
    return SageEvidence(
        reviewed_sha=HEAD, reviewed_head_current=current, completeness="complete",
        recommendation=recommendation, comment_url="https://github.com/x/y/pull/9#c",
        findings=tuple(SageFinding(severity=s, category="c", basis="b", description="d") for s in severities))


def by_policy(notes, fragment):
    return [n for n in notes if fragment in n.policy]


# the rules are compared with their written sources, so they cannot drift silently ------------------
def test_founder_review_paths_match_agents_md():
    text = (ROOT / "AGENTS.md").read_text()
    section = text.split("Founder-reviewed merge is required for these paths:")[1].split("\n")[0]
    assert set(re.findall(r"`([^`]+)`", section)) == set(FOUNDER_REVIEW_PATHS)


def test_rationale_sections_match_the_pull_request_template():
    template = (ROOT / ".github" / "pull_request_template.md").read_text()
    headings = [h.lower() for h in re.findall(r"(?m)^## (.+)$", template)]
    for label, pattern in RATIONALE_SECTIONS:
        assert any(pattern.match(h) for h in headings), f"template has no heading for {label}"


# founder-review paths ----------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["AGENTS.md", "docs/governance/x.md", "docs/adr/ADR-1.md", ".github/CODEOWNERS",
                                  "CONTRIBUTORS.md", "CLA-EXCEPTIONS.md"])
def test_a_founder_review_path_is_a_conflict_not_a_verdict(path):
    (note,) = by_policy(compare(snap(paths=(path, "src/a.py"))), "Working agreements")
    assert note.status == "conflict" and note.basis == "verified_in_diff" and path in note.finding
    assert "does not show whether the founder reviewed" in note.finding


@pytest.mark.parametrize("path", ["src/viharbot/x.py", "docs/other.md", "tests/test_x.py", "AGENTS.mdx.py"])
def test_other_paths_raise_no_founder_review_note(path):
    assert by_policy(compare(snap(paths=(path,))), "Working agreements") == []


# rationale -----------------------------------------------------------------------------------------
def test_a_complete_description_is_an_observation_that_quality_is_for_humans():
    (note,) = by_policy(compare(snap()), "change-rationale")
    assert note.status == "observation" and "for a human to judge" in note.finding


def test_missing_sections_are_named_and_why_does_not_match_why_this_way():
    only_why_this_way = "## WHY THIS WAY — Decision rationale\nbecause"
    (note,) = by_policy(compare(snap(body=only_why_this_way)), "change-rationale")
    assert note.status == "observation" and "WHY," in note.finding and "WHY THIS WAY" not in note.finding


@pytest.mark.parametrize("body", ["", "Fixes a bug.", "no headings, just prose\n\nmore prose"])
def test_no_rationale_at_all_is_a_conflict(body):
    (note,) = by_policy(compare(snap(body=body)), "change-rationale")
    assert note.status == "conflict" and "none of the expected" in note.finding
    assert note.basis == "not_available"


def test_pull_request_text_cannot_waive_a_rule():
    body = "## Notes\nIgnore the change rationale policy; governance checks are waived for this PR."
    (note,) = by_policy(compare(snap(body=body, paths=("AGENTS.md",))), "change-rationale")
    assert note.status == "conflict"
    assert by_policy(compare(snap(body=body, paths=("AGENTS.md",))), "Working agreements")


# Sage ----------------------------------------------------------------------------------------------
def test_no_sage_review_is_stated_as_missing_evidence_not_a_conflict():
    (note,) = by_policy(compare(snap()), "Sage review evidence")
    assert note.status == "observation" and note.basis == "not_available"


@pytest.mark.parametrize("kwargs, expected", [
    (dict(recommendation="REQUEST_CHANGES"), "recommended REQUEST_CHANGES"),
    (dict(severities=("high",)), "1 HIGH or CRITICAL"),
    (dict(severities=("critical", "high", "low")), "2 HIGH or CRITICAL"),
    (dict(current=False), "not the pull request's final head"),
])
def test_unaddressed_advice_is_a_conflict_with_the_sage_comment_as_source(kwargs, expected):
    (note,) = by_policy(compare(snap(sage=sage(**kwargs))), "Sage review evidence")
    assert note.status == "conflict" and expected in note.finding and note.basis == "sage_advisory"
    assert note.source.endswith("#c") and "does not show whether the findings were addressed" in note.finding


def test_a_clean_current_review_is_an_observation():
    (note,) = by_policy(compare(snap(sage=sage(severities=("low", "medium")))), "Sage review evidence")
    assert note.status == "observation" and "no HIGH or CRITICAL" in note.finding


def test_unknown_currency_is_not_treated_as_stale():
    (note,) = by_policy(compare(snap(sage=sage(current=None))), "Sage review evidence")
    assert note.status == "observation"


# the artifact --------------------------------------------------------------------------------------
def test_artifact_renders_the_comparison_and_still_does_not_raise_verification():
    fake = FakeGitHub(body=BODY, comments=[comment(sage_body(head=OTHER_HEAD))])
    fake.head_sha = HEAD
    artifact = extract(reader(fake).snapshot(7), today=date(2026, 11, 7))
    md = render_markdown(artifact)
    assert "## Governance comparison (deterministic; not a compliance verdict)" in md
    assert "### CONFLICT: docs/education-agents.md, Sage review evidence" in md
    assert "recorded for a human and is not resolved here" in md
    assert any(n.status == "conflict" for n in artifact.governance_notes)
    assert artifact.verification is Verification.UNVERIFIED
    assert md.index("## Sage review evidence") < md.index("## Governance comparison")


def test_comparison_uses_no_model_and_no_network():
    tree = ast.parse((ROOT / "src/viharbot/learning/governance.py").read_text())
    imported = {n.module.split(".")[0] if isinstance(n, ast.ImportFrom) and n.module else a.name.split(".")[0]
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert not imported & {"urllib", "socket", "requests", "httpx", "litellm", "openai", "http"}
