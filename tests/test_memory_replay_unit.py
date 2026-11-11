"""Idempotent replay authorization, without a database (issue #40, security finding on PR 1).

The PostgreSQL adapter's replay decision is exercised against a scripted transaction that returns
canned rows and records the statements it was asked to run. The real-database interleavings are in
test_memory_postgres_contract.py. Offline: no model, no gateway, no network.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pytest

pytest.importorskip("psycopg")

from memory_helpers import T0  # noqa: E402

from viharbot.memory_service.postgres import PostgresMemoryRepository  # noqa: E402
from viharbot.memory_service.repository import (  # noqa: E402
    ConflictError,
    GuardRefusedError,
    NewRecord,
    NotFoundError,
    Provenance,
    Scope,
    TombstonedError,
    WriteContext,
)

MID = "01234567-89ab-7def-8123-456789abcdef"
STORED = {"memory_id": MID, "state": "created", "rev": 1, "version": 1, "classification": "internal"}


class Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeTx:
    """Answers each named statement from a script and records the order they ran in."""

    def __init__(self, script):
        self.script = script
        self.ran: list[str] = []

    def execute(self, name, params=()):
        self.ran.append(name)
        return Cursor(self.script.get(name))


def repository(script):
    repo = PostgresMemoryRepository("postgresql://unused")
    tx = FakeTx(script)

    @contextmanager
    def fake_transaction(operation):
        yield tx

    repo._transaction = fake_transaction  # type: ignore[method-assign]
    return repo, tx


def entry(*, owner="nova", operation="create", request_hash="h", status=201, result=STORED, erased=False, memory_id=MID):
    return (operation, request_hash, status, result, erased, memory_id, owner)


def record(*, lifecycle="active", classification="internal", retention_until=None, expires_at=None):
    return (MID, lifecycle, 1, 1, classification, retention_until, expires_at)


def context(*, owner="nova", guard=None, now=T0, classes=("public", "internal")):
    return WriteContext(
        principal="nova", owner=owner, scope=Scope("agent", "nova"), request_id="r", now=now,
        guard=guard or (lambda: True), allowed_classes=classes, idempotency_key="k-0000001", request_hash="h",
    )


def new_record():
    return NewRecord(
        memory_id="99999999-89ab-7def-8123-456789abcdef", content="c",
        provenance=Provenance("agent_memory", "s", "nova", "r"), classification="internal", tags=(),
        personal_data=False, consent_basis="not_applicable", consent_reference=None,
        retention_until=T0 + timedelta(days=1), expires_at=None,
    )


def replay(script, ctx):
    repo, tx = repository({"idem_reserve": None, **script})
    return repo.create(new_record(), ctx), tx


def test_a_legitimate_replay_returns_the_stored_result_after_locking_and_guarding():
    result, tx = replay({"idem_read": entry(), "record_lock": record()}, context())
    assert (result.status, result.replayed, result.body) == (201, True, STORED)
    assert tx.ran == ["idem_reserve", "idem_read", "record_lock"]  # no write, no new record


def test_the_guard_runs_after_the_record_lock_and_a_refusal_returns_nothing_stored():
    order: list[str] = []
    repo, tx = repository({"idem_reserve": None, "idem_read": entry(), "record_lock": record()})

    def guard():
        order.append("guard@" + tx.ran[-1])
        return False

    with pytest.raises(GuardRefusedError) as error:
        repo.create(new_record(), context(guard=guard))
    assert order == ["guard@record_lock"] and MID not in str(error.value)


@pytest.mark.parametrize("stored_owner", ["scout", None])
def test_an_entry_of_another_owner_or_with_no_owner_is_refused_with_the_same_generic_error(stored_owner):
    guard_calls = []
    with pytest.raises(GuardRefusedError) as error:
        replay(
            {"idem_read": entry(owner=stored_owner), "record_lock": record()},
            context(guard=lambda: guard_calls.append(1) or True),
        )
    assert str(error.value) == GuardRefusedError.code and MID not in str(error.value)


def test_a_guard_refusal_wins_over_every_stored_state_so_nothing_about_the_entry_leaks():
    for stored in (entry(erased=True), entry(request_hash="other"), entry(status=None, result=None)):
        with pytest.raises(GuardRefusedError):
            replay({"idem_read": stored, "record_lock": record()}, context(guard=lambda: False))


@pytest.mark.parametrize("row", [
    None,                                              # erased and deleted, or never visible to this owner
    record(lifecycle="retired"),
    record(lifecycle="erased"),
    record(expires_at=T0 - timedelta(seconds=1)),
    record(retention_until=T0),                        # retention reached
    record(classification="confidential"),             # above the caller's ceiling now
])
def test_a_replay_for_a_record_that_is_no_longer_visible_returns_nothing_stored(row):
    with pytest.raises(NotFoundError) as error:
        replay({"idem_read": entry(), "record_lock": row}, context())
    assert MID not in str(error.value)


def test_a_replay_with_a_caller_that_may_touch_any_class_still_needs_an_active_record():
    result, _ = replay({"idem_read": entry(), "record_lock": record(classification="restricted")}, context(classes=None))
    assert result.replayed
    with pytest.raises(NotFoundError):
        replay({"idem_read": entry(), "record_lock": record(lifecycle="retired")}, context(classes=None))


def test_a_scrubbed_key_tombstone_is_refused_after_the_guard_and_the_owner_check():
    with pytest.raises(TombstonedError):
        replay({"idem_read": entry(erased=True, request_hash="", status=None, result=None), "record_lock": None}, context())


def test_the_same_key_for_a_different_request_is_still_a_conflict():
    with pytest.raises(ConflictError) as error:
        replay({"idem_read": entry(request_hash="other"), "record_lock": record()}, context())
    assert error.value.code == "idempotency_key_reuse"


def test_a_fresh_reservation_records_the_owner_and_skips_the_replay_path():
    repo, tx = repository({"idem_reserve": (1,), "record_insert": (MID,), "tombstone_check": None})
    repo.create(new_record(), context())
    assert tx.ran[:2] == ["idem_reserve", "record_insert"] and "idem_read" not in tx.ran
