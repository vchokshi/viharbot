# Memory service (ADR-009, issue #40, PR 1 and PR 2)

## What is wired (PR 2): Charlie only, no feature flag

**Only Charlie, the synthesis service (`AGENT_KIND=synthesis`), is integrated.** Nova (research), Scout (fact check and source quality), Cipher (security and privacy review), Pixel and Sage have **no memory integration**, and neither do the other five defined identities; a memory URL configured on any other service is ignored and logged as `agent_not_integrated`. This does not complete issue #40 or any gate.

**There is no on/off flag.** Charlie's memory client is active whenever `VIHARBOT_MEMORY_URL` is set, and inactive (logged once, content-free, not an error) when it is not.
* **Local Compose:** the URL is set (`https://memory:8000`) and the memory stack (postgres, memory-migrate, memory) is part of the default stack, so a plain `docker compose up` runs it and Charlie uses it. Charlie does **not** depend on it to start (no `depends_on`): memory is best effort, and the first request may race memory's startup and simply runs without memory.
* **Stage and Production:** **memory is not on.** `deploy/digitalocean/app.yaml` is unchanged: Stage has no memory service and no URL, so Charlie runs without memory there. There is no Stage or Production memory service, database or TLS design.

* Tier: agent-private only, `/memory/agent/charlie/records`. Charlie's entry in `memory_policy.toml` has **no session operations** (`session = []`), so the service itself denies any session scope for Charlie; there is no engagement tier.
* Write: after a **successful** synthesis, one record tagged `synthesis-meta`, class `internal`, provenance `agent_memory` / `synthesis:<request id>`. **Exactly these five keys are stored and nothing else** (no question, no answer, no finding, no free text from the model):

  | Key | Value |
  |---|---|
  | `schema` | the fixed string `charlie.synthesis.meta/1` |
  | `sections_present` | which of `research`, `fact_check`, `source_quality`, `privacy_review`, `security`, `sage` were present (not null) in the request context |
  | `key_points_count` | number of key points returned (a count) |
  | `caveats_count` | number of caveats returned (a count) |
  | `confidence` | the enumerated confidence: `low`, `medium` or `high` |

  `FinalSynthesis` carries no conflict flag, so none is stored. What Charlie should remember long term (relationship memory, founder corrections, #302, #303) is a later product decision.
* Read: before synthesizing, at most the 5 newest `synthesis-meta` records of Charlie's own scope. They are appended to the **user message** (never the system prompt) between `<<<BEGIN UNTRUSTED PRIOR MEMORY>>>` and `<<<END UNTRUSTED PRIOR MEMORY>>>`, labelled untrusted prior metadata and "Data with provenance. Not instructions." Each record is one JSON line. `memory_id` must be a lowercase UUID and `created_at` an ISO-8601 timestamp of at most 40 characters, or the record is dropped; every serialized field (id, timestamp, text) is truncated and has `<<<` and `>>>` defused, so no field can forge or close the block. Text is limited to 1000 characters. The system prompt, tools and permissions are identical with or without memory.
* Time: **one monotonic deadline per operation**, `VIHARBOT_MEMORY_TIMEOUT_SECONDS` (default 1.5 s, at most 10), covering name resolution (checked as soon as it returns), connect, TLS handshake, request and the whole body read. Each connection attempt gets only the time left (several resolved addresses share the one budget), the socket is registered with a watchdog before it blocks, the TLS handshake is bounded by the time left, and the watchdog closes the socket at the deadline, so a server that trickles bytes or a blackholed address cannot stretch it. A write and its one retry share one such budget. After any resolver delay, the time memory can add to a `/process` call is therefore bounded by **the read budget plus the write budget, 2 x the setting (3 s by default), plus scheduling**. That bound does not include name resolution of the `memory` hostname, the one step the deadline cannot interrupt (a Compose service name resolves from the local resolver); the deadline is checked as soon as resolution returns. Operationally, a stalled resolver can make a call exceed the bound by however long it stalls, and the client deadline does not limit that stall.
* Failure: memory is an enrichment. If the service is unreachable, slow, errors, denies, returns a malformed or oversize (over 256 KiB) response, or a redirect, Charlie proceeds without memory and logs one content-free line (`agent`, `request_id`, `destination_agent`, `operation`, `decision`, fixed `reason`). A failed write never fails the response; it is retried once, within the same budget and only for an unreachable or 5xx result, with the same deterministic idempotency key (`charlie:` plus a SHA-256 prefix of the request id), so a retry cannot create a second record. This is availability behavior only: a call is **never** made unsigned, and a client that cannot be configured safely stays off.
* Auth: signed with Charlie's own key and subject `charlie`; see [service authentication](service-authentication.md).
* Transport (ADR-009): `https://` is required for every non-loopback host, private networks included. A client configured with `http://` to a non-loopback host, including a private IP, refuses to start (memory off, logged). `http://` to loopback is accepted only with `VIHARBOT_MEMORY_LOCAL_DEV=on`. Redirects and environment proxies are never used (the client is `http.client` over a socket it opens itself); the URL may not carry credentials, a path or a query.
* Local Compose TLS and credentials: **one command**, `python scripts/dev_service_auth.py`, writes everything the default stack needs under `data/dev-auth/` (gitignored): the service-auth keys, the memory registry and Charlie's key, a throwaway CA certificate and a server certificate for `memory` (also `localhost`, 127.0.0.1; 30 days; the CA private key is discarded), and throwaway database passwords. The memory container serves HTTPS and the synthesis client verifies against the CA (`VIHARBOT_MEMORY_CA_FILE`). **These are development certificates and passwords. Stage and Production TLS and credentials (issuance, rotation, revocation, key custody) are not done.** If the command has not been run, postgres, memory-migrate and memory refuse to start with a message that names it; the rest of the stack is unaffected.
* First-run race (fixed here): the Postgres healthcheck now checks the TCP listener, which only the final server opens, because during first-run initialization the image's temporary server listens on the unix socket only and a socket check reported healthy before the roles existed, so `memory-migrate` could fail once on a brand-new volume.

Verification: `tests/test_charlie_memory.py` (offline; real memory app and verifier, in-memory repository, stubbed model, loopback only; its last test is the PostgreSQL write, restart, retrieve test and skips without a database). Run against a throwaway PostgreSQL container and in a separate Compose project with the real images, on a brand-new volume with a plain `docker compose up --wait` and then a restart: see the pull request. A model call was not made: the Compose check drove Charlie's memory code in the synthesis container, not `/process`.

**Status: implemented and tested offline against a local PostgreSQL. Integrated for Charlie only (PR 2, see above); no other agent calls it. Not deployed: no Stage or Production resource exists. This is PR 1 of issue #40 and does not complete the issue or any gate.** Design of record: [ADR-009](adr/ADR-009-persistent-memory.md) (accepted, PostgreSQL everywhere). Baseline and proposals: [Gate 0](memory-gate0-baseline.md). Scope language: [ADR-010](adr/ADR-010-resource-scope-language.md). Caller authentication: [ADR-006 A2](adr/ADR-006-workload-identity-and-authorization.md) and [service authentication](service-authentication.md).

`remember()` and its JSONL traces are unchanged and remain non-authoritative traces ([memory architecture](memory-architecture.md)). The service writes its own records and never reads or imports a trace.

## What is in this change

| Piece | Where |
|---|---|
| Repository interface (backend-agnostic) | `src/viharbot/memory_service/repository.py` |
| PostgreSQL adapter (psycopg 3, one connection per operation) | `src/viharbot/memory_service/postgres.py` |
| Numbered plain SQL migrations and a minimal runner | `src/viharbot/memory_service/migrations/`, `migrate.py` |
| Path parsing (ADR-010 canonical, scope only in the path) | `paths.py` |
| Memory policy table (reviewed configuration, deny by default) | `src/viharbot/execution_config/memory_policy.toml`, `policy.py` |
| Rules: limits, classification, retention, wrapping | `service.py`, `models.py` |
| FastAPI app, A2 verifier installed, audit | `app.py`, `audit.py` |
| Compose: `postgres`, `memory-migrate`, `memory` (default local stack) | `compose.yaml`, `Dockerfile.memory`, `deploy/postgres/init/10-memory-roles.sh` |

Database: one database, one `memory` schema, scope columns (`owner_agent_id`, `memory_type`, `scope_key`), not a schema per agent. Two login roles: `memory_migrator` (owns the schema, used only by the one-shot migration job) and `memory_service` (select, insert, update and delete on the record tables only; no DDL, no `TRUNCATE`, no access to migration state). No agent, and not the web tier, holds a database credential; the tests check that. Row-level security is not used (Gate 0 listed it as optional defense in depth).

## API (plain JSON REST)

Every route is addressed by path. There is no query parameter or body field that selects an agent, scope, owner or engagement; a request that carries one is refused whole.

| Method and path | Operation | Capability |
|---|---|---|
| `POST /memory/agent/<agent_id>/records` | create (service-generated id) | `memory.write` |
| `GET /memory/agent/<agent_id>/records[?tag=&limit=]` | list own active records | `memory.read` |
| `GET /memory/agent/<agent_id>/records/<memory_id>` | read one | `memory.read` |
| `POST .../records/<memory_id>/update` | new content version, needs `expected_rev` | `memory.write` |
| `POST .../records/<memory_id>/classify` | raise classification, needs `expected_rev` | `memory.write` |
| `POST .../records/<memory_id>/retire` | stop serving, keep history | `memory.write` |
| `POST .../records/<memory_id>/erase` | erase (two stages, below) | `memory.write` |
| the same under `/memory/session/<session_id>/records...` | session tier | same |
| `/memory/eng/...`, `/memory/inst/...` | reserved: 501 after authentication | |
| `GET /health` | primary reachable (open, no detail) | none |

Order of decisions: A2 assertion (401), canonical path (400, 501 for reserved), policy table and own scope (403, one generic body), query and body validation (400, 422), then the stored state's rules (404, 409, 422). Error bodies are short fixed strings that never echo input. `/docs` and the OpenAPI schema are off. Bodies are capped (default 256 KiB, `VIHARBOT_MAX_BODY_BYTES`) before authentication.

Identity is derived from the verified `sub` only: the policy table maps a `sub` to an `agent_id`, and the path scope must be that agent's own. A missing token is 401. A valid token whose `sub` is not in the table, or a path that names another agent, is 403 with the same body, so nothing about what exists is revealed. Being named in the policy is necessary, not sufficient: the key registry entry must also allow `memory.read` or `memory.write`. The service refuses to start without `VIHARBOT_KEY_REGISTRY`; it has no unauthenticated mode. `eng` and `cls` claims are required by A2 but not enforced (A3, #38).

Session ids are unverified labels that partition the caller's own records (`owner_agent_id`, `session_id`). Two agents using the same session id do not see each other's records. A real session binding waits for an engagement identity.

## Record contract

`memory_id` (UUIDv7, generated by the service, never reused), `owner_agent_id` (derived), `memory_type` (`agent_private` or `session`, derived from the path), `content`, `provenance` (`kind` and `source_id` from the caller, `author` derived from the caller's agent id, `request_id` from `X-Request-ID`), `classification`, retention and consent metadata, `created_at`, `updated_at`, `version` (content versions), `rev` (advances on every mutation) and `lifecycle` (`active`, `retired`, `erased`).

* **Classification.** Scale: `public`, `internal`, `confidential`, `restricted`. Each agent has a ceiling in the policy table, `internal` for every shipped entry (the same as the knowledge broker, which refuses `confidential`; this reconciles Gate 0's classification conflict by making the broker's rule the default ceiling). A writer may raise a record up to its ceiling and never lower it. A record above the caller's current ceiling is invisible to it for read, update and classify; its owner can still retire and erase it.
* **Retention and consent.** Agent-private records carry `retention_until` (default 90 days, at most 365 per policy). Session records carry `expires_at` (default 1 hour, at most 24 hours). Both are enforced at read time and for writes (`update` and `classify`; `retire` and `erase` deliberately do not judge expiry, so an owner can still remove an expired record). Write-time expiry is judged against a fresh service-clock time read after the record lock is acquired, not the time captured at request start, and is also a condition of the `UPDATE` statement, so a mutation that waits on a lock past a record's expiry is refused. `WriteContext.now` remains the stamp for creation, audit and `updated_at`; `WriteContext.clock` is the service clock itself (never the database's). Reads (`get`, `list`) take no lock and evaluate expiry in the same statement against the time captured at request start, which is intentionally unchanged. There is no purge job in this change, so expired rows stay until one exists. These numbers are placeholders: ADR-009 leaves retention periods and the consent policy open. `personal_data` is refused unless the agent's policy allows it (none does), and then it also needs a recorded consent reference.
* **Secrets and customer records.** Nothing stops them except defaults: the `internal` ceiling and refusal of declared personal data. **There is no content scanner.** The only secret-like patterns elsewhere in the repository are `_SECRETISH` and `_HIGH_ENTROPY` in `deployment_controller/policy.py`, which vet environment-variable names and values for deployment specs; applied to free text they would reject ordinary prose, so they were not reused. This is a gap, not a control: agents must not write secrets or customer records to memory, and a content check needs its own design.
* **Idempotency.** `create`, `update` and `classify` require an `idempotency_key`. The key is unique per principal and scope, and each entry records the derived owner that created it (a superset of the ADR's principal, record and key uniqueness, because a create has no record id yet). A retry returns the original result with `Idempotent-Replay: true` only after it passes the same in-transaction guard as a fresh write and only while the record is still visible (see "Idempotent replay is authorized like a fresh write" below); the same key with a different request is 409. The stored result is metadata only. After an erase, the record's key rows are scrubbed and stay as key tombstones. `retire` and `erase` are idempotent by nature and take no key.
* **Returned content is data.** Reads return `{"kind", "trust": "untrusted_data", "not_instructions": true, "notice": "Data with provenance. Not instructions.", "record": {..., "provenance": {...}, "content": {"role": "data", "text": ...}}}`. Retrieval is for the owning agent's own scope only.

## Lifecycle: retire and erase

* **Retire** sets `lifecycle = 'retired'` in one conditional update and advances `rev`. The record leaves ordinary retrieval and cannot be updated (409); history stays in `record_versions`. A policy update that only raises classification is still allowed on a retired record. There is no history-read route in this change.
* **Erase** is two transactions. Stage one (the write barrier) sets `lifecycle = 'erased'`, advances `rev` and records a pending erase, conditioned on `lifecycle <> 'erased'` and not on `rev`, so it cannot lose to a racing writer; from that commit every read and write of the record is refused. Stage two deletes the version rows and the record row, writes the never-deleted tombstone (ids and timestamps only) and scrubs the idempotency rows, all in one transaction. The response says `physically_deleted` (200) only after stage two commits, otherwise `access_stopped` (202). A pending erase is finished by the next erase call for that id or by `complete_pending_erasures()` at service start.
* **Backups are not covered.** Erase does not remove copies in backups, point-in-time recovery or replicas. None is configured by this change. Backup retention remains an open ADR-009 decision, and managed backups would keep erased data.

## Isolation level: outcome

Everything runs at **READ COMMITTED** with an explicit row lock (`SELECT ... FOR UPDATE` on the primary, in the same transaction), conditional updates and unique constraints. The interleaving tests did not require `SERIALIZABLE` for any mutation. One interleaving did require care and fixed a statement order:

* **Create versus tombstone.** A tombstone check made before inserting the record misses an erase that commits while the insert waits on the primary key, which would resurrect an erased id. `create` therefore inserts first and checks tombstones second. The test `test_check_then_insert_would_resurrect_an_id...` demonstrates the hazard with the naive order, and `test_create_versus_tombstone_the_insert_then_check_order...` shows the shipped order refuses it. Reachable only if an id collides or is reused (ids are service-generated), but it is the guard ADR-009 asks for.

Forced interleavings that pass: two writers on one record (the second waits on the lock and loses on `rev`); writer versus erase in both orders; writer versus retire in both orders; a classification raise between a read and a write; a policy table change between authorization and the write (the guard runs inside the transaction after the lock, so it refuses). The guard is bound to the policy the request was validated against: for `create`, `update` and `classify` it refuses unless the caller's current policy equals that policy, so a lowered classification ceiling, content, retention or TTL limit, a revoked personal-data permission, or a sub remapped to another agent (which authorization alone would pass on a session path) refuses the write, and a retry validates against the new policy. `retire` and `erase` carry no validated payload, so they only require that the same agent still owns the caller's identity; an unrelated limit change does not stop an erasure; a duplicate idempotency key racing itself (one record, the second returns the first's result); a stale writer and a fresh writer after an erase; the guarded `UPDATE` refusing a stale or erased writer even with the service check bypassed; a mutation holding a lock a second connection cannot take; a session or agent record that expires while an `update` or `classify` waits on its lock, which is refused and leaves the record unchanged (with a control where it does not expire). Mutation checks confirmed the tests bite: removing `FOR UPDATE` fails ten of them, and swapping the create order fails three.

**Idempotent replay is authorized like a fresh write.** A replay is not returned before the guard. Once the transaction finds a committed entry for the key, it runs, in this order: (1) the entry's `owner_agent_id` must equal the caller's current derived owner, else the generic 403 (an entry with no recorded owner also fails closed); (2) the record the entry refers to is locked (`FOR UPDATE`, in the caller's own scope); (3) the same in-transaction guard as a fresh write runs against the live policy, else the same generic 403 with nothing about the stored result; (4) only then the scrubbed-key (409 `key_tombstoned`), reused-key (409 `idempotency_key_reuse`) and incomplete-entry checks run; (5) the stored result is returned only if the record is still visible to the caller: active, within retention or expiry, and inside the caller's classification ceiling. Expiry and retention are judged at a fresh service-clock time read after the record lock and the guard (as update and classify do), not at the request-start time, because the wait on the idempotency reservation or the lock can outlast the record's expiry; the request-start time is used only for stamps. If it is not (retired, erased and pending deletion, expired, above the ceiling now), the replay is a 404 `not found` and returns no stored metadata. The tests cover each step, including a policy revocation that lands while a duplicate waits on the first request's key.

*Decision for a legitimate retry after the record has gone.* A retry after a lost response still returns the original outcome with `Idempotent-Replay: true` whenever the record is still visible, including after later updates (the stored result is the original one, for example `rev` 1 of a create). If the record has since been retired, erased, expired or classified above the caller's ceiling, the retry gets 404, the same answer a read gets, rather than the stored metadata. The cost is that a client whose create succeeded and then expired or was retired before its retry arrives sees 404 and must treat the record as gone; it cannot learn that its first attempt had succeeded. That is deliberate: returning a `memory_id` and classification for a record the caller can no longer see would reveal state the read path withholds.

*Ownership.* Migration `0002_idempotency_owner.sql` adds a nullable `owner_agent_id` column to `memory.idempotency`, written at reservation from the derived owner, and checked on replay. Chosen over putting the owner in the key because it needs no change to the primary key and no rewrite of existing rows: the migration is one additive column, backfilled from the entry's record (or its tombstone, for erased keys). An existing entry that cannot be backfilled keeps a null owner and is refused on replay (fail closed); its caller gets a 403 and must use a new key. A consequence of the key staying principal plus scope plus key: a sub remapped to a different agent can never reuse an old key of its predecessor, it gets the generic 403 instead of a fresh write, and must pick a new key.

Not proven: behavior under a read replica or standby (the service must connect to the primary only, which is configuration), under `SERIALIZABLE`, or on Stage and Production instances (ADR-009 requires the same suite there before adoption).

### Transaction shapes

`TRANSACTION_SHAPES` in `postgres.py` lists the exact statements per mutation, and the tests assert that each recorded run equals it, that every mutation locks before it writes (create claims the primary key instead), and that each write carries its guard.

| Mutation | Statements, in order | Lock | Guard on the write |
|---|---|---|---|
| create | reserve key, insert record, check tombstone, insert version, complete key | primary key insert | `ON CONFLICT (memory_id) DO NOTHING`; tombstone check after |
| update | reserve key, lock row, update record, insert version, complete key | `FOR UPDATE` | `WHERE rev = $n AND lifecycle = 'active'` |
| classify | reserve key, lock row, update record, complete key | `FOR UPDATE` | `WHERE rev = $n AND lifecycle <> 'erased'` |
| retire | lock row, update record | `FOR UPDATE` | `WHERE lifecycle = 'active'` |
| erase, stage one | lock row, mark erased, insert erase request | `FOR UPDATE` | `WHERE lifecycle <> 'erased'` (no `rev`) |
| erase, stage two | lock row, delete versions, delete record, insert tombstone, scrub keys, complete request | `FOR UPDATE OF r` | `DELETE ... WHERE lifecycle = 'erased'` |

## Audit and logs

Every read, write and denial is one JSON line from the `memory_audit` logger: service, request id, verified caller, scope, operation, outcome and a fixed reason code, plus the record id where there is one. Authentication denials are logged by `service_auth` and again at memory level. The audit call has no parameter that could carry content, a token or a key, and a test scans all captured logs for planted content, provenance, tokens and key seeds. Database errors are logged by class only. Logs are the audit trail; there is no audit table in this change.

## Running it locally

Part of the default local stack, so a plain `docker compose up` runs it. One setup command first (it is idempotent and keeps existing material; `--force` replaces keys, registry and certificates, never the database passwords):

```bash
python scripts/dev_service_auth.py   # service-auth keys, the memory registry and Charlie's key, a throwaway TLS CA and server certificate, and throwaway database passwords, all under data/dev-auth/ (gitignored)
docker compose up -d --build --wait
```

The passwords are read from `data/dev-auth/memory-db/*.env` (one file per role; the service never sees the migration role's password) and are never in `compose.yaml`. The files are `required: false` so `docker compose config` works before setup; without them the passwords are empty and postgres, the init script and the memory service refuse to start with a message that names the setup command. The init script runs only when the data volume is empty; change a password later with `ALTER ROLE`. The registry entry for a caller needs `sub` equal to a `subs` value in `memory_policy.toml` and `capabilities = ["memory.read", "memory.write"]`; the audience is `memory`. The memory service listens on **HTTPS** inside the Compose network (it is not published to the host). Its server certificate comes from the throwaway development CA created by the same command, for the hostname `memory`; Charlie's client verifies the connection against that CA (`VIHARBOT_MEMORY_CA_FILE`) and refuses a plain-HTTP non-loopback URL, as ADR-009 requires. A wrong CA fails closed (no request is sent). **These are development certificates for local Compose; Stage and Production TLS and credential designs are not finished.**

Database passwords are handled apart from keys and certificates, because Postgres applies a role's password only when it first initializes its data volume. If all three files in `data/dev-auth/memory-db/` exist they are kept (also by `--force`); if none exist all three are generated; if only some exist the command refuses, changes nothing, and tells you to restore the missing file or remove the memory data volume (`docker compose down -v`, or `docker volume rm <project>_memory_pg_data`) and run it again. `python scripts/dev_service_auth.py --rotate-database-passwords` is the one explicit way to replace them; an existing volume keeps the old role passwords, so recreate the volume or `ALTER ROLE` afterwards.

## Running the contract tests

The PostgreSQL tests are selected by `VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN` (a superuser DSN for a throwaway server) and skip with a message naming it when it is unset. Each session creates the two roles and a fresh database, runs the real migrations, and drops the database after.

```bash
docker run -d --name vb-pg-test -e POSTGRES_PASSWORD=<throwaway> -p 127.0.0.1:55432:5432 postgres:17.11-alpine3.24
export VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN="postgresql://postgres:<throwaway>@127.0.0.1:55432/postgres"
export VIHARBOT_MEMORY_TEST_PG_CONTAINER=vb-pg-test   # optional: also restarts the container mid-test
python -m pytest tests/test_memory_service_unit.py tests/test_memory_deployment_static.py tests/test_memory_postgres_contract.py
```

The unit and static files need no database.

## CI: what can and cannot run

**Decision (founder, 2026-11-10): CI does not run PostgreSQL.** The database contract tests are not run in CI. Developers run them locally against a throwaway container (set `VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN`, and `VIHARBOT_MEMORY_TEST_PG_CONTAINER` for the restart test), and they are run against the Stage instance before it is adopted, as ADR-009's conformance rule requires. They therefore **skip in CI**, and nothing automatic catches a regression in the locking, migration or erase logic between a local run and Stage; the pull request that changes that code must say it was run against a database. Two points:

1. **Done in this pull request.** In the **Docker job** the offline suite runs in the test image with no network, and a skip fails the job unless its message matches `--allow-skip`. The database tests and the optional restart test skip there by design (the container has no network), so the "Require that only the opt-in live checks were skipped" step in `ci.yml` now uses
   `--allow-skip 'RUN_LITELLM_(INTEGRATION|INFERENCE)=1 to|VIHARBOT_MEMORY_TEST_PG_(ADMIN_DSN|CONTAINER) is not set'`.
2. **Not done, by decision.** If this is ever reversed, to make the **Python job** run them, add to `python-tests` a `services:` block and one env var on the test step. Postgres service: `image: postgres:17.11-alpine3.24`, `env: POSTGRES_PASSWORD: <throwaway literal, job-local>`, `ports: ["5432:5432"]`, `options: --health-cmd "pg_isready -U postgres" --health-interval 5s --health-timeout 3s --health-retries 10`; on "Run tests with coverage": `VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN: postgresql://postgres:<same literal>@127.0.0.1:5432/postgres`. The container is ephemeral and unreachable from outside the job, but a literal password in a workflow may trip Gitleaks, and `docs/continuous-integration.md` says CI uses no secrets, so the founder should decide how to word it. The image tag was checked on Docker Hub on 2026-11-10 and is pinned by tag, not digest.

Coverage: because CI has no database, the adapter's SQL paths are not executed there, which lowers measured coverage; see the pull request for the measured figure against the 90% floor.

## Driver and license

`psycopg[binary]==3.3.8` (psycopg 3.3.8 with psycopg-binary 3.3.8, the current stable release on PyPI, uploaded 2026-11-04, `requires-python >=3.10`; psycopg.org lists 3.3.9 as unreleased). The license is **LGPL-3.0-only** (PyPI `license_expression`) for both packages; the binary wheel bundles libpq and its dependencies, which were not individually audited. It is a pinned, unmodified, pip-installed dependency used through its public API, isolated in the optional `memory` extra and the test extra, so it is not installed in the web or other agent images. LGPL obligations (notices, letting a recipient replace the library) attach mainly to distributing an image or package that bundles it. This repository is proprietary with no license granted and CLA-based relicensing intended (see [CONTRIBUTING](../CONTRIBUTING.md)), no lawyer has reviewed anything, and the repository's dependency review has no license gate. **Accepted by the founder on 2026-11-10.** That is a business decision, not legal advice. Revisit it before ViharBot images or installers are distributed to customers or outside contributors, because that is when LGPL distribution obligations could apply, and have counsel confirm at that point. If it is ever reversed, `postgres.py` and `migrate.py` are the only modules that import the driver.

## What is not done

* No agent calls the service and nothing is wired into execution. PR 2 is separate.
* A2 is not on Research, Security, Sage or Pixel (#235, being done separately and before PR 2); the memory service verifies callers itself.
* No engagement-shared or institutional tier, cross-scope promotion, semantic search, object storage, history-read route, purge job for expired rows, content scanner, row-level security, or connection pool.
* No Stage or Production provisioning, no TLS termination, no backups, no change to `deploy/digitalocean/app.yaml`.
* A3 (delegation grants, #38), engagement identity and per-record ACLs do not exist, so only an agent's own scope can be authorized.
