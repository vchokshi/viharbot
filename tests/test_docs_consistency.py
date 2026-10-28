"""Documentation checks: links resolve, the streaming reference matches the code, diagrams are sane.

These read only repository files. They do not call any model or network.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import get_args

import pytest

import viharbot.live_events as live
from viharbot.live_events import EVENT_TYPES, TERMINAL_EVENT_TYPES, parse_event

ROOT = Path(__file__).resolve().parent.parent
STREAM_DOC = ROOT / "docs" / "live-observability.md"
FENCE = re.compile(r"```(\w*)\n(.*?)```", re.S)

# The Docker test image does not ship docs/ (see development-status.md); these checks need the repo.
pytestmark = pytest.mark.skipif(not STREAM_DOC.exists(), reason="docs/ is not in this environment")


def markdown_files() -> list[Path]:
    tracked = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True)
    if tracked.returncode != 0 or not tracked.stdout.strip():
        pytest.skip("not a git checkout")
    return [ROOT / name for name in tracked.stdout.split() if (ROOT / name).exists()]


def prose(path: Path) -> str:
    return FENCE.sub("", path.read_text(encoding="utf-8"))


def test_every_relative_markdown_link_resolves():
    broken = []
    for path in markdown_files():
        for target in re.findall(r"\]\(([^)\s]+)\)", prose(path)):
            if re.match(r"^(https?:|mailto:|#)", target):
                continue
            file_part = target.split("#", 1)[0]
            if file_part and not (path.parent / file_part).resolve().exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
    assert broken == []


def registry_rows() -> dict[str, tuple[str, str, bool]]:
    rows = {}
    for line in STREAM_DOC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 6 and re.fullmatch(r"`[a-z_]+`", cells[0]) and cells[0].strip("`") in EVENT_TYPES:
            rows[cells[0].strip("`")] = (cells[1], cells[2], "yes" in cells[5])
    return rows


def data_fields(event_type: str) -> tuple[list[str], list[str]]:
    for name in dir(live):
        cls = getattr(live, name)
        if isinstance(cls, type) and issubclass(cls, live._Envelope) and cls is not live._Envelope:
            if get_args(cls.model_fields["event_type"].annotation)[0] == event_type:
                fields = cls.model_fields["data"].annotation.model_fields
                return ([k for k, v in fields.items() if v.is_required()],
                        [k for k, v in fields.items() if not v.is_required()])
    raise AssertionError(event_type)


def test_the_streaming_registry_lists_exactly_the_events_in_the_code():
    rows = registry_rows()
    assert tuple(rows) == EVENT_TYPES and len(rows) == 14


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_each_registry_row_matches_the_schema_fields_and_terminal_flag(event_type):
    required, optional = data_fields(event_type)
    doc_required, doc_optional, terminal = registry_rows()[event_type]
    assert re.findall(r"`(\w+)`", doc_required) == required
    assert re.findall(r"`(\w+)`", doc_optional) == optional
    assert terminal == (event_type in TERMINAL_EVENT_TYPES)


def test_every_streaming_example_validates_and_each_event_has_exactly_one():
    seen = []
    for lang, body in FENCE.findall(STREAM_DOC.read_text(encoding="utf-8")):
        if lang != "json":
            continue
        event = json.loads(body)
        parse_event(event)  # the real schema
        seen.append(event["event_type"])
    assert sorted(seen) == sorted(EVENT_TYPES)


def test_the_documented_sse_constants_match_the_code():
    from viharbot import live_stream
    text = STREAM_DOC.read_text(encoding="utf-8")
    assert f"{int(live_stream.KEEPALIVE_SECONDS)} seconds" in text
    assert f"{live_stream.MAX_CONCURRENT_STREAMS} simultaneous" in text
    assert f"bounded ({live_stream.QUEUE_SIZE})" in text
    assert f"at most {int(live_stream.SINK_TIMEOUT_SECONDS)} seconds" in text
    assert live.SCHEMA_VERSION == "1.0" and '`"1.0"`' in text


DIAGRAMS = ("flowchart", "sequenceDiagram", "graph", "classDiagram", "stateDiagram")


def test_mermaid_diagrams_have_a_known_type_and_balanced_blocks():
    problems = []
    for path in markdown_files():
        for lang, body in FENCE.findall(path.read_text(encoding="utf-8")):
            if lang != "mermaid":
                continue
            first = body.strip().splitlines()[0].split()[0]
            if first not in DIAGRAMS:
                problems.append(f"{path.name}: unknown diagram type {first}")
            if first not in ("flowchart", "graph"):
                continue  # in other diagram types `end` closes loop/alt blocks, not subgraphs
            opens = len(re.findall(r"^\s*subgraph\b", body, re.M))
            closes = len(re.findall(r"^\s*end\s*$", body, re.M))
            if opens != closes:
                problems.append(f"{path.name}: {opens} subgraph vs {closes} end")
    assert problems == []


def test_the_baseline_documents_exist_and_are_linked_from_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in ("docs/development-status.md", "docs/audits/phase-6.5-architecture-audit.md",
                 "docs/live-observability.md", "docs/architecture.md"):
        assert (ROOT / name).exists() and name in readme
