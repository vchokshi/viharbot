"""Issue #140: Archie works only on explicit founder assignments. Offline; no model, no network."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from test_learning_pipeline import INS, SRC, FakeGitHub, proposer, reader
from viharbot.learning import ingest
from viharbot.learning.assignment import (
    AssignmentState, AssignmentStore, NotAssigned, NotFounder, founders_from_env,
)
from viharbot.learning.models import IngestState
from viharbot.learning.store import IngestionStore

FOUNDER = "vchokshi"


def store_for(tmp_path, founders=frozenset({FOUNDER})):
    return AssignmentStore(tmp_path / "assignments", founders)


def test_only_a_configured_founder_can_assign(tmp_path):
    store = store_for(tmp_path)
    with pytest.raises(NotFounder):
        store.assign("someone-else", "t", SRC, (7,))
    assert store.list() == []
    assert store.assign("VChokshi", "t", SRC, (7,)).assigned_by == FOUNDER  # case-insensitive login


def test_no_founder_configured_means_nothing_can_be_assigned(tmp_path):
    with pytest.raises(NotFounder):
        store_for(tmp_path, frozenset()).assign(FOUNDER, "t", SRC, (7,))
    assert founders_from_env("") == frozenset()
    assert founders_from_env("a-b, bad login!, C") == frozenset({"a-b", "c"})


@pytest.mark.parametrize("prs", [(), (0,), (7, 7), tuple(range(1, 30))])
def test_assignment_scope_is_validated(tmp_path, prs):
    with pytest.raises(ValueError):
        store_for(tmp_path).assign(FOUNDER, "t", SRC, prs)


def test_unassigned_pull_request_creates_no_lesson_and_makes_no_call(tmp_path):
    fake = FakeGitHub()
    store = IngestionStore(tmp_path / "learning")
    with pytest.raises(NotAssigned):
        ingest.process(7, reader(fake), store, store_for(tmp_path), proposer(fake))
    assert fake.calls == [] and store.list() == []  # refused before any network call or record


def test_assignment_covers_only_its_listed_pull_requests_and_repository(tmp_path):
    asg = store_for(tmp_path)
    asg.assign(FOUNDER, "t", SRC, (7,))
    assert asg.authorizing(SRC, 7) and not asg.authorizing(SRC, 8) and not asg.authorizing(INS, 7)


def test_reconcile_with_no_assignment_does_nothing(tmp_path):
    fake = FakeGitHub()
    store = IngestionStore(tmp_path / "learning")
    assert ingest.reconcile(reader(fake), store, store_for(tmp_path), proposer(fake)) == []
    assert [c for c in fake.calls if c[0] != "GET"] == [] and store.list() == []


def test_assigned_work_runs_and_is_traceable_to_the_assignment(tmp_path):
    fake, asg = FakeGitHub(), store_for(tmp_path)
    a = asg.assign(FOUNDER, "agent identity", SRC, (7,))
    event = ingest.process(7, reader(fake), IngestionStore(tmp_path / "learning"), asg, None)
    assert event.state is IngestState.EXTRACTED and event.assignment_id == a.assignment_id


def test_cancelled_assignment_stops_work_and_cannot_be_reopened(tmp_path):
    asg = store_for(tmp_path)
    a = asg.assign(FOUNDER, "t", SRC, (7,))
    with pytest.raises(NotFounder):
        asg.cancel("someone-else", a.assignment_id)
    assert asg.cancel(FOUNDER, a.assignment_id).state is AssignmentState.CANCELLED
    assert asg.complete(FOUNDER, a.assignment_id).state is AssignmentState.CANCELLED  # stays closed
    fake = FakeGitHub()
    with pytest.raises(NotAssigned):
        ingest.process(7, reader(fake), IngestionStore(tmp_path / "learning"), asg, None)
    assert fake.calls == []


def test_cancelling_mid_run_blocks_publication(tmp_path):
    fake, asg = FakeGitHub(), store_for(tmp_path)
    a = asg.assign(FOUNDER, "t", SRC, (7,))

    class Cancelling:
        def propose(self, *args, **kwargs):  # must never be reached
            raise AssertionError("published after cancellation")

    real_extract = ingest.extract

    def extract_then_cancel(snapshot):
        artifact = real_extract(snapshot)
        asg.cancel(FOUNDER, a.assignment_id)
        return artifact

    ingest.extract = extract_then_cancel
    try:
        event = ingest.process(7, reader(fake), IngestionStore(tmp_path / "learning"), asg, Cancelling())
    finally:
        ingest.extract = real_extract
    assert event.state is IngestState.FAILED and event.error == "NotAssigned"
    assert [c for c in fake.calls if c[0] != "GET"] == []


def test_assignment_ids_cannot_escape_the_store(tmp_path):
    with pytest.raises(KeyError):
        store_for(tmp_path).get("../../etc/passwd")


def test_nothing_in_the_pipeline_can_create_an_assignment():
    """Event, webhook and ingestion code may read assignments but never create or close them."""
    for name in ("ingest.py", "github.py", "extract.py", "store.py"):
        text = Path("src/viharbot/learning", name).read_text()
        for call in (".assign(", ".cancel(", ".complete("):
            assert call not in text, f"{name} calls {call}"


def _cli():
    spec = importlib.util.spec_from_file_location("archie_learn", "scripts/archie_learn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_assign_cancel_and_refusals(monkeypatch, tmp_path, capsys):
    cli = _cli()
    monkeypatch.setenv("ASSIGNMENT_STORE_DIR", str(tmp_path / "a"))
    monkeypatch.setenv("LEARNING_STORE_DIR", str(tmp_path / "l"))
    monkeypatch.setenv("ARCHIE_SOURCE_TOKEN", "t")
    monkeypatch.setenv("ARCHIE_FOUNDER_LOGINS", FOUNDER)
    assert cli.main(["assign", "--as", "intruder", "--topic", "x", "--pr", "7"]) == 2
    assert cli.main(["assign", "--as", FOUNDER, "--topic", "x", "--pr", "7"]) == 0
    assignment_id = capsys.readouterr().out.split(":")[0]
    assert cli.main(["cancel", "--as", "intruder", assignment_id]) == 2
    assert cli.main(["cancel", "--as", FOUNDER, assignment_id]) == 0
    assert cli.main(["ingest", "--pr", "7"]) == 2  # cancelled: refused before any network call


def test_cancelling_the_authorising_assignment_blocks_publication_despite_an_overlapping_one(tmp_path):
    """Sage HIGH on #247: publication must be re-checked against the assignment that started the work."""
    fake, asg = FakeGitHub(), store_for(tmp_path)
    asg.assign(FOUNDER, "t1", SRC, (7,))
    asg.assign(FOUNDER, "t2", SRC, (7,))  # a second, still-active assignment covering the same PR
    authorising = asg.authorizing(SRC, 7)  # the one `process` will capture (the choice is deterministic)

    class Never:
        def propose(self, *args, **kwargs):
            raise AssertionError("published after the authorising assignment was cancelled")

    real_extract = ingest.extract

    def extract_then_cancel(snapshot):
        artifact = real_extract(snapshot)
        asg.cancel(FOUNDER, authorising.assignment_id)
        return artifact

    ingest.extract = extract_then_cancel
    try:
        event = ingest.process(7, reader(fake), IngestionStore(tmp_path / "learning"), asg, Never())
    finally:
        ingest.extract = real_extract
    assert asg.authorizing(SRC, 7) is not None  # the other assignment is still active
    assert event.state is IngestState.FAILED and event.error == "NotAssigned"
    assert event.assignment_id == authorising.assignment_id
    assert [c for c in fake.calls if c[0] != "GET"] == []


def test_cli_reconcile_refusal_exits_cleanly(monkeypatch, tmp_path, capsys):
    """`reconcile` has no --pr; a NotAssigned mid-run must exit 2, not raise AttributeError."""
    cli = _cli()
    monkeypatch.setenv("ASSIGNMENT_STORE_DIR", str(tmp_path / "a"))
    monkeypatch.setenv("LEARNING_STORE_DIR", str(tmp_path / "l"))
    monkeypatch.setenv("ARCHIE_SOURCE_TOKEN", "t")

    def cancelled_mid_run(*args, **kwargs):
        raise NotAssigned("cancelled during the run")

    monkeypatch.setattr(cli, "reconcile", cancelled_mid_run)
    assert cli.main(["reconcile"]) == 2
    assert "refused" in capsys.readouterr().err
