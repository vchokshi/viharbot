# Memory platform readiness report (issue #40, Part of #40)

This is an evidence report, not a decision and not an ADR. Baseline: `main` at `a84f203`, branch
`test/memory-platform-gaps`. Date: 2026-11-16. It covers `src/viharbot/memory_service/` except
`stage_entry.py` (reviewed separately). Code is authority; where this report and a document
disagree, the code wins.

**Status words.** PASS means the check was executed in this review and passed; the evidence column
names the command or test. NOT RUN means it was not executed here. REVIEWED means the code was read
and found consistent with the claim but no test run proves it by itself. Nothing is marked PASS from
reading alone.

## What ran

| Run | Environment | Result |
|---|---|---|
| `python -m pytest -q` (no PostgreSQL variable set) | local, Python 3.13.9 | 4036 passed, 110 skipped (baseline 3962 passed, 92 skipped; +74 passed, +18 skipped = the new database tests, which skip offline) |
| `tests/test_memory_*.py`, `tests/test_charlie_memory.py`, `tests/test_stage_memory_materials.py` with `VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN` pointing at a throwaway local `postgres:16` container on `127.0.0.1:55432` | local Docker, superuser admin | 565 passed, 0 skipped; the cluster was empty again afterwards (no leaked databases or roles) |

Not run anywhere in this review: any test against DigitalOcean Managed PostgreSQL, any TLS
connection to a server certificate (`sslmode=verify-full` against a real CA), any cloud resource, any
real model or gateway call, the Stage launcher end to end, backup or restore behaviour, load or
soak testing, and a non-superuser admin (managed-cluster shape) for the database fixtures.

## Findings

| # | Area | Claim | Status | Evidence |
|---|---|---|---|---|
| 1 | Schema ownership | The migrator owns the `memory` schema and every table in it; the service role owns no object | PASS | `test_the_migrator_owns_the_schema_and_every_table_and_the_service_role_owns_nothing` (pg_class owner and pg_shdepend), local PostgreSQL 16 |
| 2 | Role privileges | The service role holds exactly the documented table grants (no TRUNCATE, REFERENCES or TRIGGER; no access to `schema_migrations`; schema USAGE only, no CREATE); `PUBLIC` has nothing on the schema or tables | PASS | `test_the_service_role_holds_exactly_the_documented_privileges`, `test_public_has_no_access_to_the_memory_schema`; existing `test_the_service_role_has_no_ddl_truncate_or_migration_access` (also checks not superuser, no createdb or createrole) |
| 3 | Migration idempotency | Applying again is a no-op; a changed applied file is refused; stray names and gaps are refused; the pre-0002 database upgrades and backfills owners | PASS | `test_migrations_apply_once_and_a_changed_applied_file_is_refused`, `test_a_gap_or_reordered_migration_set_is_refused_before_anything_runs`, `test_a_database_with_pre_migration_idempotency_rows_still_replays_for_its_original_owner`, `test_running_migrate_twice_is_a_no_op_the_second_time` |
| 4 | Migration transactions | A failing migration rolls back its DDL and bookkeeping row together, and the advisory lock is released | PASS (new) | `test_a_failing_migration_rolls_back_its_ddl_and_its_bookkeeping_row_and_frees_the_lock` |
| 5 | Migration serialization | Two runners started together apply a pending migration exactly once | PASS (new) | `test_two_runners_started_together_apply_a_pending_migration_exactly_once` (threads, 1.5 s migration, one result `[]`, one result with the file) |
| 6 | Database constraints | The database itself refuses malformed rows (session without expiry, agent record with expiry, scope not equal to owner, consent without reference, bad class or lifecycle, rev 0, unknown type, null owner, content empty or over 65536 bytes, orphan version) | PASS (new) | `test_the_database_refuses_a_record_row_that_breaks_the_model` (9 cases), `test_the_database_bounds_content_at_65536_bytes_and_refuses_empty_content` |
| 7 | Write idempotency | A retry returns the original result with `Idempotent-Replay: true`; a different request under a used key is a 409; racing duplicates create one record; replay is authorized like a write (owner, ceiling, lifecycle, expiry, policy) | PASS | existing contract tests (`test_an_idempotent_retry_returns_the_original_result`, `test_a_duplicate_idempotency_key_racing_itself_creates_one_record`, `test_a_different_request_under_a_used_key_is_a_conflict_not_a_second_write`, the replay group) and `tests/test_memory_replay_unit.py` |
| 8 | Idempotency hash | The request hash ignores only the key and binds operation, tier, scope, record id and body | PASS (new) | `test_the_request_hash_ignores_only_the_idempotency_key`, `test_an_update_hash_binds_the_record_id_and_expected_rev` |
| 9 | Retry after a lock timeout | A lock timeout is a generic 503, changes nothing, writes no idempotency row, and the same-key retry then succeeds exactly once and replays after that | PASS (new) | `test_a_lock_timeout_is_a_generic_503_that_leaves_the_record_untouched_and_the_retry_succeeds` |
| 10 | Generated ids | A colliding id is redrawn and never reused; three collisions give a generic 503; an idempotency conflict is not retried with a new id | PASS (new) | `test_a_colliding_generated_id_is_redrawn_and_never_reused`, `test_three_colliding_ids_end_in_a_generic_503`, `test_an_idempotency_conflict_is_a_409_and_is_not_retried_with_a_new_id` |
| 11 | Expiry | Expired records are never served or written; expiry is strict (`expires_at > now`), so a record is unserved at the exact instant of expiry; an expired record can still be erased | PASS | `test_expired_and_stale_records_are_never_served`, `test_a_record_is_unserved_at_the_exact_instant_of_its_expiry` (new), the `waiter` tests for expiry during a lock wait |
| 12 | Retention and TTL derivation | Defaults and limits come from the policy and the service clock; naive, past, equal-to-now and over-maximum retention, over-maximum TTL, wrong-tier fields and oversize content (counted in bytes) are refused before storage | PASS (new, offline) | `tests/test_memory_service_validation_unit.py` (create refusals, boundaries, defaults) |
| 13 | Retention enforcement | Expired rows are never physically deleted by a job | NOT IMPLEMENTED (known) | `docs/memory-service.md` lists "purge job for expired rows" as not built; ADR-009 describes deletion after retention. Read-time enforcement is what exists and is tested (row 11). This is a limitation to record, not a defect in what runs |
| 14 | Replay handling | Replay after retire, erase, expiry, owner remap, lowered ceiling, or revoked policy returns nothing stored; replay after a later update still returns the original | PASS | existing contract tests (`test_a_replay_after_*`, `test_a_sub_remapped_*`, `test_a_lowered_ceiling_*`, `test_a_legitimate_replay_is_unchanged_*`) |
| 15 | Concurrent access | Writers versus writers, erase, retire and policy change resolve by row lock, `rev` and in-transaction guard; locks are taken before writes; isolation is READ COMMITTED | PASS | existing contract tests (about 25 interleaving tests, deterministic via a gate), run in the 565-test run |
| 16 | Agent-private isolation | An agent reaches only its own scope; wrong agent, forged `sub`, unnamed `sub`, missing token get a generic refusal; the same session id is a separate partition per agent; scope in query or body is refused | PASS | `test_wrong_agent_forged_sub_missing_token_and_unnamed_sub_reach_nothing`, `test_the_same_session_id_is_a_separate_partition_per_agent`, `test_scope_in_the_path_versus_a_conflicting_query_or_body_writes_nothing`, `tests/test_memory_service_unit.py` |
| 17 | Malformed input | Malformed JSON, invalid UTF-8, arrays, null, deep nesting, lone surrogate, NUL, empty or whitespace content give a generic 422 and never reach storage; malformed list queries (zero, over-limit, non-ASCII digits, duplicates, unknown names, bad tag) give 400 | PASS (new) | `test_a_malformed_create_body_is_a_generic_422_that_never_reaches_storage` (10 cases), `test_a_malformed_list_query_is_a_400_and_never_reaches_storage` (15 cases) |
| 18 | Error bodies | Storage errors map to fixed status and body with no detail; 503 body is exactly `temporarily unavailable`; database failure carries no connection detail | PASS | `test_storage_errors_map_to_fixed_bodies_that_carry_no_detail` (new), `test_a_database_failure_is_a_generic_503_that_carries_no_detail`, `test_an_unexpected_error_is_a_generic_500_that_never_echoes_its_message` |
| 19 | Health | `/health` is open and reports 503 without detail when storage is down | PASS | `test_health_reports_unavailable_without_detail_when_storage_is_down` (new), `test_health_is_open_and_reports_the_primary` |
| 20 | Charlie contract | Charlie stores exactly five fields (`schema`, and the four metadata fields in `docs/memory-service.md`) and reads at most its five newest own records | PASS | `tests/test_charlie_memory.py` (exact-dict assertion at the write test and the five-record read test), run green in the 565-test run |
| 21 | DB connection and TLS | `strict_tls` forces `verify-full` with a CA file and refuses a non-loopback host without one; credentials have no defaults; connect, lock and statement timeouts are set per connection | PASS for the settings and the refusal logic; NOT RUN against a real TLS server | `tests/test_memory_stage_entry.py` (conninfo cases), `tests/test_memory_service_unit.py::test_database_settings_have_no_defaults_for_credentials`, `test_the_isolation_level_is_read_committed_on_every_connection`. No certificate handshake was exercised |
| 22 | Non-strict TLS default | Outside the Stage launcher, `MEMORY_DB_SSLMODE` defaults to `prefer` and any value is passed through | REVIEWED | `conninfo_from_environment`. Acceptable for Compose on a private network (documented in its docstring); it must not be the path used for any non-loopback production database. No code change made |
| 23 | Duplicate JSON keys | A body with a repeated key is parsed last-value-wins | REVIEWED, behaviour observed | `CreateRequest.model_validate_json` returned the later `content`. The signed body hash covers the raw bytes, so the assertion still binds what was sent; no defect. Pinned by `test_duplicate_json_keys_cannot_smuggle_a_second_value` |
| 24 | Managed PostgreSQL / non-superuser admin | The fixtures and migrations work with a DigitalOcean-style non-superuser admin | NOT RUN | only a local superuser cluster was available |

## Defects found

1. **Test defect, fixed** (`tests/test_memory_migrate_schema.py`, commit "migrate-schema fixture cleans up its own databases and roles"). Six database tests failed on a real PostgreSQL at teardown: the fixture created `migtest_` databases but removed them through `memory_helpers.drop_database`, which refuses any name it did not create, so each run also leaked a database and roles. The fixture also created the cluster-wide role `memory_service` and never removed it, and it ran on remote clusters despite needing that real role name. It now drops exactly what it made, removes `memory_service` only if it created it, and skips on remote clusters. Before: 340 passed, 6 failed. After: all pass and the cluster is empty afterwards. These tests had never run in the offline suite (they skip without a database), which is why CI did not see it.

No defect was found in `src/viharbot/memory_service/` (repository, postgres, migrations, policy, service, app, audit, migrate); no source change was made.

## Observations for the owners of other files (not changed here)

* `stage_entry.py`, `stage_bootstrap.py`, `deploy/digitalocean/app.yaml` and `tests/test_memory_deployment_static.py` were out of scope and not reviewed.
* Findings 13 and 22 are the two limitations most worth a line in `docs/development-status.md` if not already there (docs are owned by another agent).

## How to reproduce

Offline: `python -m pytest -q`. Database tests: start a throwaway local PostgreSQL (for example `docker run --rm -e POSTGRES_PASSWORD=<pw> -p 127.0.0.1:55432:5432 postgres:16`), set `VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN=postgresql://postgres:<pw>@127.0.0.1:55432/postgres`, then run `python -m pytest -q tests/test_memory_*.py`. See the `tests/memory_helpers.py` docstring for the remote-cluster guard.
