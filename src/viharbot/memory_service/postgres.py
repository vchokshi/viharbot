"""PostgreSQL adapter for the memory repository (ADR-009, amended for PostgreSQL).

Isolation: every transaction runs at READ COMMITTED with explicit row locks and conditional
updates, as in ADR-009's PostgreSQL mapping. The interleaving tests decide whether any mutation
needs more; the outcome is recorded in docs/memory-service.md. Mechanics, per mutation:

* one transaction per mutation, one connection per operation (no pool is added);
* the record row is locked with ``SELECT ... FOR UPDATE`` on the primary, inside the transaction,
  and authorization is decided from that row, never from an older version or a cache;
* content writes publish with ``UPDATE ... WHERE rev = $n AND lifecycle = 'active'`` and zero rows
  updated means the writer lost;
* the erase barrier is conditioned on ``lifecycle <> 'erased'`` and deliberately not on ``rev``;
* a unique constraint on (principal, scope, idempotency key) holds the original result;
* create inserts the row first and checks tombstones second, because under READ COMMITTED a check
  made before the insert can miss an erase that commits while the insert waits on the key.

Every statement is a named constant in ``SQL`` and goes through ``_Transaction.execute`` so the
transaction-shape tests can list exactly what each mutation runs and which locks it takes.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Sequence

import psycopg
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from .policy import classification_rank
from .repository import (
    ConflictError,
    GuardRefusedError,
    MemoryRepository,
    MemoryStoreError,
    MutationResult,
    NewRecord,
    NewVersion,
    NotFoundError,
    Provenance,
    Record,
    RefusedError,
    Scope,
    StorageUnavailableError,
    TombstonedError,
    WriteContext,
)

logger = logging.getLogger("memory_store")

ISOLATION = psycopg.IsolationLevel.READ_COMMITTED
ISOLATION_NAME = "READ COMMITTED"

_COLUMNS = (
    "r.memory_id, r.memory_type, r.scope_key, r.owner_agent_id, r.classification, r.lifecycle, "
    "r.rev, r.version, r.tags, r.personal_data, r.consent_basis, r.consent_reference, "
    "r.retention_until, r.expires_at, r.created_at, r.updated_at, "
    "v.content, v.prov_kind, v.prov_source_id, v.prov_author, v.prov_request_id"
)
# Read-time enforcement of lifecycle, retention, expiry and the caller's classification ceiling.
_VISIBLE = (
    "r.lifecycle = 'active' "
    "AND (r.retention_until IS NULL OR r.retention_until > %s) "
    "AND (r.expires_at IS NULL OR r.expires_at > %s) "
    "AND r.classification = ANY(%s)"
)
_SCOPE = "r.owner_agent_id = %s AND r.memory_type = %s AND r.scope_key = %s"
_IDEM_KEY = "principal = %s AND scope_type = %s AND scope_key = %s AND idempotency_key = %s"

SQL: dict[str, str] = {
    "ping": "SELECT 1",
    # --- reads: one statement each, authorization decided from the current row on the primary ---
    "record_get": (
        f"SELECT {_COLUMNS} FROM memory.records r JOIN memory.record_versions v "
        f"ON v.memory_id = r.memory_id AND v.version = r.version "
        f"WHERE {_SCOPE} AND r.memory_id = %s AND {_VISIBLE}"
    ),
    "record_list": (
        f"SELECT {_COLUMNS} FROM memory.records r JOIN memory.record_versions v "
        f"ON v.memory_id = r.memory_id AND v.version = r.version "
        f"WHERE {_SCOPE} AND {_VISIBLE} AND (%s::text IS NULL OR %s = ANY(r.tags)) "
        f"ORDER BY r.created_at DESC, r.memory_id DESC LIMIT %s"
    ),
    # --- idempotency ---
    "idem_reserve": (
        "INSERT INTO memory.idempotency "
        "(principal, scope_type, scope_key, idempotency_key, operation, request_hash, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1"
    ),
    "idem_read": (
        "SELECT operation, request_hash, status_code, result, erased FROM memory.idempotency "
        f"WHERE {_IDEM_KEY}"
    ),
    "idem_complete": (
        "UPDATE memory.idempotency SET memory_id = %s, status_code = %s, result = %s "
        f"WHERE {_IDEM_KEY}"
    ),
    "idem_scrub": (
        "UPDATE memory.idempotency SET erased = true, request_hash = '', status_code = NULL, "
        "result = NULL WHERE memory_id = %s"
    ),
    # --- the authoritative locking read: the row lock that serializes mutations of one record ---
    "record_lock": (
        "SELECT memory_id, lifecycle, rev, version, classification, retention_until, expires_at "
        "FROM memory.records WHERE memory_id = %s AND owner_agent_id = %s AND memory_type = %s "
        "AND scope_key = %s FOR UPDATE"
    ),
    # --- create ---
    "record_insert": (
        "INSERT INTO memory.records (memory_id, memory_type, scope_key, owner_agent_id, "
        "classification, lifecycle, rev, version, tags, personal_data, consent_basis, "
        "consent_reference, retention_until, expires_at, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, 'active', 1, 1, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (memory_id) DO NOTHING RETURNING memory_id"
    ),
    "tombstone_check": "SELECT 1 FROM memory.tombstones WHERE memory_id = %s",
    "tombstone_owned": (
        "SELECT 1 FROM memory.tombstones WHERE memory_id = %s AND owner_agent_id = %s "
        "AND memory_type = %s AND scope_key = %s"
    ),
    "version_insert": (
        "INSERT INTO memory.record_versions (memory_id, version, content, prov_kind, "
        "prov_source_id, prov_author, prov_request_id, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    ),
    # --- the conditional updates (zero rows updated means the writer lost) ---
    "record_update_content": (
        "UPDATE memory.records SET rev = rev + 1, version = version + 1, updated_at = %s, "
        "classification = %s, tags = COALESCE(%s::text[], tags) "
        "WHERE memory_id = %s AND rev = %s AND lifecycle = 'active' "
        "AND (retention_until IS NULL OR retention_until > %s) "
        "AND (expires_at IS NULL OR expires_at > %s)"
    ),
    "record_update_classification": (
        "UPDATE memory.records SET rev = rev + 1, classification = %s, updated_at = %s "
        "WHERE memory_id = %s AND rev = %s AND lifecycle <> 'erased' "
        "AND (retention_until IS NULL OR retention_until > %s) "
        "AND (expires_at IS NULL OR expires_at > %s)"
    ),
    "record_retire": (
        "UPDATE memory.records SET rev = rev + 1, lifecycle = 'retired', retired_at = %s, "
        "updated_at = %s WHERE memory_id = %s AND lifecycle = 'active'"
    ),
    # The erase barrier: conditioned on lifecycle and deliberately NOT on rev, so it cannot lose.
    "record_erase_mark": (
        "UPDATE memory.records SET rev = rev + 1, lifecycle = 'erased', erased_at = %s, "
        "updated_at = %s WHERE memory_id = %s AND lifecycle <> 'erased'"
    ),
    "erase_request_insert": (
        "INSERT INTO memory.erase_requests (memory_id, owner_agent_id, memory_type, scope_key, "
        "reason, state, requested_at, request_id) VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s) "
        "ON CONFLICT (memory_id) DO NOTHING"
    ),
    # --- erase completion (stage two) ---
    "erase_lock": (
        "SELECT r.owner_agent_id, r.memory_type, r.scope_key, e.request_id "
        "FROM memory.records r JOIN memory.erase_requests e ON e.memory_id = r.memory_id "
        "WHERE r.memory_id = %s AND r.lifecycle = 'erased' FOR UPDATE OF r"
    ),
    "erase_versions_delete": "DELETE FROM memory.record_versions WHERE memory_id = %s",
    "erase_record_delete": "DELETE FROM memory.records WHERE memory_id = %s AND lifecycle = 'erased'",
    "tombstone_insert": (
        "INSERT INTO memory.tombstones (memory_id, owner_agent_id, memory_type, scope_key, "
        "erased_at, request_id) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (memory_id) DO NOTHING"
    ),
    "erase_request_complete": (
        "UPDATE memory.erase_requests SET state = 'physically_deleted', completed_at = %s "
        "WHERE memory_id = %s AND state = 'pending'"
    ),
    "erase_pending": (
        "SELECT memory_id FROM memory.erase_requests WHERE state = 'pending' ORDER BY requested_at"
    ),
}

# The statements each mutation runs, in order, on its success path with an idempotency key where
# the operation takes one. The tests assert the recorded statements equal this, and that every
# write is guarded as the ADR requires. A reviewer adding a mutation adds its row here.
TRANSACTION_SHAPES: dict[str, tuple[str, ...]] = {
    "get": ("BEGIN", "record_get", "COMMIT"),
    "list": ("BEGIN", "record_list", "COMMIT"),
    "create": (
        "BEGIN", "idem_reserve", "record_insert", "tombstone_check", "version_insert",
        "idem_complete", "COMMIT",
    ),
    "update": (
        "BEGIN", "idem_reserve", "record_lock", "record_update_content", "version_insert",
        "idem_complete", "COMMIT",
    ),
    "classify": (
        "BEGIN", "idem_reserve", "record_lock", "record_update_classification", "idem_complete",
        "COMMIT",
    ),
    "retire": ("BEGIN", "record_lock", "record_retire", "COMMIT"),
    "erase": ("BEGIN", "record_lock", "record_erase_mark", "erase_request_insert", "COMMIT"),
    "complete_erasure": (
        "BEGIN", "erase_lock", "erase_versions_delete", "erase_record_delete", "tombstone_insert",
        "idem_scrub", "erase_request_complete", "COMMIT",
    ),
}


class _Transaction:
    """One database transaction. Records every statement into the optional log."""

    def __init__(self, conn: psycopg.Connection, operation: str, log: list | None) -> None:
        self._conn = conn
        self._operation = operation
        self._log = log
        self._record("BEGIN", "BEGIN")

    def _record(self, name: str, sql: str) -> None:
        if self._log is not None:
            self._log.append((self._operation, name, sql))

    def execute(self, name: str, params: Sequence[Any] = ()) -> psycopg.Cursor:
        sql = SQL[name]
        self._record(name, sql)
        return self._conn.execute(sql, params)

    def finish(self, outcome: str) -> None:
        self._record(outcome, outcome)


class PostgresMemoryRepository(MemoryRepository):
    def __init__(
        self,
        conninfo: str,
        *,
        statement_log: list | None = None,
        lock_timeout_ms: int = 10_000,
        statement_timeout_ms: int = 15_000,
        connect_timeout_s: int = 5,
    ) -> None:
        self._conninfo = conninfo
        self._log = statement_log
        self._options = (
            f"-c lock_timeout={int(lock_timeout_ms)} -c statement_timeout={int(statement_timeout_ms)}"
        )
        self._connect_timeout = connect_timeout_s

    @contextmanager
    def _transaction(self, operation: str) -> Iterator[_Transaction]:
        try:
            conn = psycopg.connect(
                self._conninfo, autocommit=False, connect_timeout=self._connect_timeout,
                options=self._options,
            )
        except psycopg.Error as exc:
            logger.error("memory store connect failed", extra={"agent": "memory", "error": type(exc).__name__})
            raise StorageUnavailableError() from None
        try:
            conn.isolation_level = ISOLATION
            tx = _Transaction(conn, operation, self._log)
            try:
                yield tx
            except MemoryStoreError:
                conn.rollback()
                tx.finish("ROLLBACK")
                raise
            except psycopg.Error as exc:
                conn.rollback()
                tx.finish("ROLLBACK")
                # The class only: a driver message can carry key values, never log it.
                logger.error(
                    "memory store error",
                    extra={"agent": "memory", "operation_class": operation, "error": type(exc).__name__},
                )
                raise StorageUnavailableError() from None
            except BaseException:
                conn.rollback()
                tx.finish("ROLLBACK")
                raise
            else:
                conn.commit()
                tx.finish("COMMIT")
        finally:
            conn.close()

    # --- reads ------------------------------------------------------------------------------

    def ping(self) -> None:
        with self._transaction("ping") as tx:
            tx.execute("ping").fetchone()

    def get(self, scope, owner, memory_id, now, allowed_classes):
        with self._transaction("get") as tx:
            row = tx.execute(
                "record_get",
                (owner, scope.memory_type, scope.key, memory_id, now, now, list(allowed_classes)),
            ).fetchone()
        return _record(row) if row else None

    def list(self, scope, owner, now, allowed_classes, tag, limit):
        with self._transaction("list") as tx:
            rows = tx.execute(
                "record_list",
                (owner, scope.memory_type, scope.key, now, now, list(allowed_classes), tag, tag, limit),
            ).fetchall()
        return [_record(row) for row in rows]

    # --- shared pieces of a mutation --------------------------------------------------------

    def _idempotency_begin(self, tx: _Transaction, ctx: WriteContext, operation: str) -> MutationResult | None:
        """Reserve the key in this transaction, or return the original committed result."""
        if ctx.idempotency_key is None:
            return None
        key = (ctx.principal, ctx.scope.tier, ctx.scope.key, ctx.idempotency_key)
        reserved = tx.execute(
            "idem_reserve", (*key, operation, ctx.request_hash, ctx.now)
        ).fetchone()
        if reserved:
            return None
        # Another transaction committed this key first (ours waited on the unique index).
        existing = tx.execute("idem_read", key).fetchone()
        if existing is None:  # cannot happen: the conflicting row was committed
            raise ConflictError("idempotency_unavailable")
        stored_operation, stored_hash, status, result, erased = existing
        if erased:
            raise TombstonedError("key_tombstoned")
        if stored_operation != operation or stored_hash != ctx.request_hash:
            raise ConflictError("idempotency_key_reuse")
        if status is None or result is None:
            raise ConflictError("idempotency_unavailable")
        return MutationResult(status, result, replayed=True)

    def _idempotency_complete(self, tx: _Transaction, ctx: WriteContext, memory_id: str, result: MutationResult) -> None:
        if ctx.idempotency_key is None:
            return
        tx.execute(
            "idem_complete",
            (memory_id, result.status, Jsonb(result.body), ctx.principal, ctx.scope.tier,
             ctx.scope.key, ctx.idempotency_key),
        )

    def _lock(self, tx: _Transaction, memory_id: str, ctx: WriteContext):
        """Lock the record in the caller's own scope. None when it is not there for this caller."""
        return tx.execute(
            "record_lock", (memory_id, ctx.owner, ctx.scope.memory_type, ctx.scope.key)
        ).fetchone()

    @staticmethod
    def _expired(row, now: datetime) -> bool:
        return (row[5] is not None and row[5] <= now) or (row[6] is not None and row[6] <= now)

    @staticmethod
    def _in_class_range(row, ctx: WriteContext) -> bool:
        return ctx.allowed_classes is None or row[4] in ctx.allowed_classes

    @staticmethod
    def _meta(memory_id: str, state: str, rev: int, version: int, classification: str) -> dict:
        return {
            "memory_id": memory_id, "state": state, "rev": rev, "version": version,
            "classification": classification,
        }

    # --- mutations --------------------------------------------------------------------------

    def create(self, new: NewRecord, ctx: WriteContext) -> MutationResult:
        with self._transaction("create") as tx:
            replay = self._idempotency_begin(tx, ctx, "create")
            if replay:
                return replay
            if not ctx.guard():
                raise GuardRefusedError()
            inserted = tx.execute(
                "record_insert",
                (new.memory_id, ctx.scope.memory_type, ctx.scope.key, ctx.owner, new.classification,
                 list(new.tags), new.personal_data, new.consent_basis, new.consent_reference,
                 new.retention_until, new.expires_at, ctx.now, ctx.now),
            ).fetchone()
            if inserted is None:
                raise ConflictError("id_exists")
            # After the insert, never before: see the module docstring.
            if tx.execute("tombstone_check", (new.memory_id,)).fetchone():
                raise TombstonedError("id_tombstoned")
            _insert_version(tx, new.memory_id, 1, new.content, new.provenance, ctx.now)
            result = MutationResult(201, self._meta(new.memory_id, "created", 1, 1, new.classification))
            self._idempotency_complete(tx, ctx, new.memory_id, result)
            return result

    def update(self, memory_id, expected_rev, change: NewVersion, ctx: WriteContext) -> MutationResult:
        with self._transaction("update") as tx:
            replay = self._idempotency_begin(tx, ctx, "update")
            if replay:
                return replay
            row = self._lock(tx, memory_id, ctx)
            # Expiry is judged at a fresh service-clock time read after the lock, not ctx.now: the
            # lock wait can last up to lock_timeout, and the record may expire while we wait.
            checked_at = ctx.clock()
            if row is None or row[1] == "erased" or self._expired(row, checked_at) or not self._in_class_range(row, ctx):
                raise NotFoundError()
            _, lifecycle, rev, version, classification = row[:5]
            if lifecycle != "active":
                raise ConflictError("not_active")
            if rev != expected_rev:
                raise ConflictError("rev_mismatch")
            new_class = classification
            if change.classification is not None:
                new_class = _not_lowered(classification, change.classification)
            if not ctx.guard():
                raise GuardRefusedError()
            updated = tx.execute(
                "record_update_content",
                (ctx.now, new_class, list(change.tags) if change.tags is not None else None,
                 memory_id, expected_rev, checked_at, checked_at),
            )
            if updated.rowcount != 1:  # the writer lost: re-read and re-authorize
                raise ConflictError("rev_mismatch")
            _insert_version(tx, memory_id, version + 1, change.content, change.provenance, ctx.now)
            result = MutationResult(
                200, self._meta(memory_id, "updated", rev + 1, version + 1, new_class)
            )
            self._idempotency_complete(tx, ctx, memory_id, result)
            return result

    def classify(self, memory_id, expected_rev, classification, ctx: WriteContext) -> MutationResult:
        with self._transaction("classify") as tx:
            replay = self._idempotency_begin(tx, ctx, "classify")
            if replay:
                return replay
            row = self._lock(tx, memory_id, ctx)
            checked_at = ctx.clock()  # fresh, after the lock: see update
            if row is None or row[1] == "erased" or self._expired(row, checked_at) or not self._in_class_range(row, ctx):
                raise NotFoundError()
            _, lifecycle, rev, version, current = row[:5]
            if rev != expected_rev:
                raise ConflictError("rev_mismatch")
            _strictly_raised(current, classification)  # raises RefusedError unless strictly higher
            if not ctx.guard():
                raise GuardRefusedError()
            updated = tx.execute(
                "record_update_classification",
                (classification, ctx.now, memory_id, expected_rev, checked_at, checked_at),
            )
            if updated.rowcount != 1:
                raise ConflictError("rev_mismatch")
            result = MutationResult(
                200, self._meta(memory_id, "classified", rev + 1, version, classification)
            )
            self._idempotency_complete(tx, ctx, memory_id, result)
            return result

    def retire(self, memory_id, ctx: WriteContext) -> MutationResult:
        with self._transaction("retire") as tx:
            row = self._lock(tx, memory_id, ctx)
            if row is None or row[1] == "erased":
                raise NotFoundError()
            _, lifecycle, rev, version, classification = row[:5]
            if lifecycle == "retired":  # idempotent: the record is already out of service
                return MutationResult(200, self._meta(memory_id, "retired", rev, version, classification), replayed=True)
            if not ctx.guard():
                raise GuardRefusedError()
            updated = tx.execute("record_retire", (ctx.now, ctx.now, memory_id))
            if updated.rowcount != 1:
                raise ConflictError("not_active")
            return MutationResult(200, self._meta(memory_id, "retired", rev + 1, version, classification))

    def erase(self, memory_id, reason, ctx: WriteContext) -> MutationResult:
        with self._transaction("erase") as tx:
            row = self._lock(tx, memory_id, ctx)
            if row is None:
                if tx.execute(
                    "tombstone_owned", (memory_id, ctx.owner, ctx.scope.memory_type, ctx.scope.key)
                ).fetchone():
                    return MutationResult(200, {"memory_id": memory_id, "state": "physically_deleted"}, replayed=True)
                raise NotFoundError()
            _, lifecycle, rev, version, classification = row[:5]
            if lifecycle == "erased":  # the barrier is already up; completion may still be pending
                return MutationResult(202, {"memory_id": memory_id, "state": "access_stopped"}, replayed=True)
            if not ctx.guard():
                raise GuardRefusedError()
            marked = tx.execute("record_erase_mark", (ctx.now, ctx.now, memory_id))
            if marked.rowcount != 1:  # cannot happen under the row lock; fail closed
                raise ConflictError("not_active")
            tx.execute(
                "erase_request_insert",
                (memory_id, ctx.owner, ctx.scope.memory_type, ctx.scope.key, reason, ctx.now, ctx.request_id),
            )
            return MutationResult(202, {"memory_id": memory_id, "state": "access_stopped"})

    def complete_erasure(self, memory_id: str, now: datetime) -> bool:
        with self._transaction("complete_erasure") as tx:
            row = tx.execute("erase_lock", (memory_id,)).fetchone()
            if row is None:
                return bool(tx.execute("tombstone_check", (memory_id,)).fetchone())
            owner, memory_type, scope_key, request_id = row
            tx.execute("erase_versions_delete", (memory_id,))
            tx.execute("erase_record_delete", (memory_id,))
            tx.execute("tombstone_insert", (memory_id, owner, memory_type, scope_key, now, request_id))
            tx.execute("idem_scrub", (memory_id,))
            tx.execute("erase_request_complete", (now, memory_id))
            return True

    def pending_erasures(self) -> list[str]:
        with self._transaction("pending_erasures") as tx:
            return [str(row[0]) for row in tx.execute("erase_pending").fetchall()]


def _not_lowered(current: str, requested: str) -> str:
    """The requested class, which may equal the current one but never be lower (ADR-009)."""
    if classification_rank(requested) < classification_rank(current):
        raise RefusedError("classification_lowered")
    return requested


def _strictly_raised(current: str, requested: str) -> str:
    if classification_rank(requested) < classification_rank(current):
        raise RefusedError("classification_lowered")
    if requested == current:
        raise RefusedError("classification_not_raised")
    return requested


def _insert_version(tx: _Transaction, memory_id: str, version: int, content: str, prov: Provenance, now: datetime) -> None:
    tx.execute(
        "version_insert",
        (memory_id, version, content, prov.kind, prov.source_id, prov.author, prov.request_id, now),
    )


def _record(row) -> Record:
    (memory_id, memory_type, scope_key, owner, classification, lifecycle, rev, version, tags,
     personal_data, consent_basis, consent_reference, retention_until, expires_at, created_at,
     updated_at, content, prov_kind, prov_source_id, prov_author, prov_request_id) = row
    return Record(
        memory_id=str(memory_id), memory_type=memory_type, scope_key=scope_key, owner_agent_id=owner,
        content=content, provenance=Provenance(prov_kind, prov_source_id, prov_author, prov_request_id),
        classification=classification, tags=tuple(tags), personal_data=personal_data,
        consent_basis=consent_basis, consent_reference=consent_reference,
        retention_until=retention_until, expires_at=expires_at, created_at=created_at,
        updated_at=updated_at, version=version, rev=rev, lifecycle=lifecycle,
    )
