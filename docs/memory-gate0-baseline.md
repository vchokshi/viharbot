# Memory Gate 0: baseline and decisions for issue #40

**Status: baseline and proposal. Nothing here is implemented.** Measured on `main` at `58993f2` on 2026-11-10. No tests were run for this document (it is documentation only), and no stored memory content was read: `data/` is not in the repository checkout, and nothing below quotes a record. The design of record is [ADR-009](adr/ADR-009-persistent-memory.md), which is **Proposed** (amended 2026-11-10 for PostgreSQL) until the founder accepts it. Issue #40 cites it as "ADR-003", which does not exist; the issue text is not edited.

Settled inputs (not reopened here): PostgreSQL in every environment, bodies in the row, DynamoDB approved but not used for memory, DigitalOcean for Stage and AWS or GCP for Production, cross-cloud placement only under the conditions in [datastore-platform.md](datastore-platform.md), ADR-006 and ADR-010 Accepted, and a flexible team of about 8 to 12 agents (counts below come from the registry and `bindings.toml`).

## 1. Inventory of memory paths

### Writes

| Path | Evidence | What it writes |
|---|---|---|
| `remember(agent, request_id, result)` | `src/viharbot/memory.py:12-21` | Appends `{timestamp, request_id, result}` to `$MEMORY_DIR/<agent>/memory.jsonl` (default root `data`). No locking, no idempotency, no scope beyond the agent string |
| Research | `src/viharbot/research_agent.py:79` | `remember("research", ...)` with the structured findings |
| Security | `src/viharbot/security_agent.py:70` | `remember("security", ...)` with the assessment |
| Specialists (fact-check, source-quality, privacy-review, synthesis) | `src/viharbot/specialist_agent.py:227` | `remember(AGENT_KIND, ...)` with the serialized result (fact-check after `guard_fact_check`) |
| Sage | `src/viharbot/sage_agent.py:92` | `remember("sage", ...)` with the serialized analysis |
| Pixel deck state | `src/viharbot/pixel/state.py:23`, `src/viharbot/pixel_agent.py:55` | Files under `$MEMORY_DIR/pixel/<deck_id>` and `.../pixel/decks` (newest `PIXEL_MAX_DECKS`); written atomically by tmp-and-replace (`pixel/state.py:26-30`). This is task state, not agent memory |
| Institutional entries | `src/viharbot/knowledge/store.py:32-37` | One JSON file per entry plus an identifier-only `audit.log` (`store.py:43-46`). **No production code imports it**: the only importer is `tests/test_institutional_memory.py`. It is an unwired, tested module |

`live_stream._remember` (`src/viharbot/live_stream.py:123`) is **not memory**. It stores event IDs in an in-process dict (`self._ids`) so later stream events can cite earlier ones. It is neither a write path nor a trace, and the name is misleading.

### Reads

There are no read paths for `memory.jsonl`. Nothing reads these files back (`docs/memory-architecture.md`; confirmed by the absence of any reader in `src/`). The only retrieval code is `KnowledgeBroker.retrieve` (`knowledge/broker.py:131-152`), which is unwired. Pixel reads its own deck state for its own tasks.

### Mounts and persistence

| Environment | Evidence | Behavior |
|---|---|---|
| Local Compose, Research and Security | `compose.yaml:76-79`, `98-100` | Bind mounts `./data/research` and `./data/security`. Persist on the host |
| Local Compose, fact-check, source-quality, privacy-review, synthesis, pixel, sage | `compose.yaml:121-123`, `217-219`, `242-244` and the matching lines for the others; volumes declared at `compose.yaml:306-` | Named volumes at `/data`. Persist across restarts |
| DigitalOcean Stage | `deploy/digitalocean/app.yaml:98-99`, `124`, `154`, `184`, `214`, `244` | `MEMORY_DIR=/tmp/viharbot-memory` for six agents. **Ephemeral**: App Platform's disk is lost on redeploy ([DigitalOcean limits](https://docs.digitalocean.com/products/app-platform/details/limits/)) |
| Tests | `compose.yaml:296`; `scripts/fact_check_eval/run.py:123` | Temporary directories |

### Traces versus reusable memory

Everything `remember()` writes is an **audit-style trace of one run's output**, keyed only by request ID. Nothing is designed to be reused, so none of it is "memory" in the #40 sense. Proposal: keep `remember()` as a non-authoritative trace (ADR-009 Consequences allows this) and build memory as a separate path, so a trace is never mistaken for a governed record.

## 2. Agents that have an execution binding

From `src/viharbot/execution_config/bindings.toml`: nine bindings across six identities. The registry defines nine identities in total; the rest have no execution binding and run nothing.

| Identity | Service(s) | Stage (`app.yaml`) | Dev stack | Writes memory today |
|---|---|---|---|---|
| Nova | research | yes | yes | `remember("research")` |
| Scout | fact-check, source-quality | yes | yes | `remember` via `specialist_agent.py:227` |
| Cipher | security, privacy-review | yes | yes | `remember("security")`; privacy-review via the specialist path |
| Charlie | synthesis | yes | yes | specialist path |
| Pixel | pixel (slide generation) | no | yes | deck state files only |
| Sage | sage, and sage-code-review (in-process, GitHub review adapter only) | no | yes | `remember("sage")` |

Stage runs six services (`app.yaml` components: web plus research, security, fact-check, source-quality, privacy-review, synthesis). Unbound identities (for example Atlas, Orbit, Kepler) cannot be tested for memory and must be reported as not operational rather than counted. Counts here are a snapshot; the registry and `bindings.toml` are the source of truth.

## 3. Prerequisites and what authorization can enforce today

| Prerequisite | Current state (evidence) | Consequence for #40 |
|---|---|---|
| ADR-009 | Proposed, amended for PostgreSQL | Founder must accept it as amended before PR 1 is merged as the design of record |
| ADR-006 | Accepted. A2 (authentication) and A2b (outbound assertions) implemented, off by default | The memory service can verify callers only if it adopts the A2 verifier and is configured with a key registry |
| A2 coverage | Only `specialist_agent.py:134-136` installs the verifier (`require_assertion`). Research, Security, Sage and Pixel verify nothing (`docs/service-authentication.md`; #235 open) | Four of the memory-writing services cannot yet prove who they are to a memory service unless #235 is done or they are only reached through a verifying path |
| A3 (#38) | Not implemented. `eng` and `cls` claims are present but not enforced; no grants | Engagement and classification **cannot be enforced from tokens yet** |
| #39 | Open | Inter-agent authentication is partial |
| #48 | Open | The poisoning and isolation tests for memory do not exist yet and must be built with PR 3 |
| Engagement identity | Web tier has one shared login (`docs/architecture.md:152`); ADR-006 open question 4 | There is no real "engagement" or "user" to scope memory to, so engagement-shared memory cannot be authorized meaningfully yet |

**Conclusion.** Service-boundary authorization can today prove a caller's identity (only where A2 is on) and enforce a route capability from the registry. It cannot enforce engagement, classification or grant scope, and most memory-writing services cannot authenticate yet. A memory service must therefore **deny by default**: a request with no verified `sub`, or a `sub` not named in a repository-configured memory policy table, gets nothing. For PR 1 this means the first tiers shipped are the ones that need no engagement identity (see section 6).

## 4. Proposed data contract and trust boundaries

Memory types: **session** (expires, never promoted), **agent-private** (durable, keyed to the logical agent), **explicitly shared engagement** (needs an engagement identity that does not exist yet, so deferred). Institutional is the existing broker's tier and is out of core scope here.

| #40 field | ADR-009 field | Existing `KnowledgeEntry` field | Notes and conflicts |
|---|---|---|---|
| `memory_id` | `pk`/record id (service-generated) | `entry_id` (content hash) | **Conflict:** the broker derives the id from the content, so the same claim always has one id; ADR-009 uses a service-generated id plus idempotency keys. Different semantics; keep both and do not mix |
| `owner_agent_id` | `owner`, `agent_scope` (derived from token) | `agent_scope` ("set by the broker, not the caller") | Compatible. Derive from the verified `sub`, never from the body |
| `subject/user` | `authorized_principals[]` | none | No user identity exists (single login). Field stays optional and unused until one does |
| `tenant/engagement scope` | `engagement_scope`, `eng#<id>` | none | Broker has no engagement concept. Deferred |
| `memory_type` | taxonomy type | `topic` | Not the same: `topic` is a subject, not a lifecycle class |
| `content` | `body` | `claim` | The broker refuses `confidential` at the door |
| `provenance` | `provenance{source, author, request_id}` | `Provenance{kind, source_id, author, request_id}` | Close. Adopt the broker's `kind` and `source_id` |
| `classification` | `classification` with ceiling rules | `classification` ("internal"; "confidential" refused) | **Conflict:** ADR-009 allows classes up to a ceiling and forbids lowering; the broker allows only `internal`. Reconcile in PR 1 |
| `consent/retention` | `retention_until`, erase/retire | none | Missing in the broker |
| `timestamps` | `created`/`updated`, `validated_at` | `created_at`, `approval.approved_at` | Compatible |
| `version/tombstone` | `version`, `rev`, `lifecycle` (`active`/`retired`/`erased`), tombstones | none | Missing. **Conflict in status words:** broker `retired`; ADR-009 workflow `superseded` and a separate `lifecycle` |
| Access control | `authorized_principals[]` ACL on the record | `GrantTable`: `requester`, exact `topics`, `agent_scopes`; default deny; no wildcards | Different models: record ACL versus requester grant. The grant table is reviewed configuration and has no engagement dimension |
| Audit | audit every read and write, identifiers only | `audit.log`, identifiers only | Compatible |

**Path-addressed resource naming (so ADR-010 scopes apply).** Proposal:

- `/memory/agent/<agent_id>/records/<memory_id>` for agent-private records (`<agent_id>` taken from the verified token, so a scope `/memory/agent/nova/*` is a real boundary).
- `/memory/session/<session_id>/records/<memory_id>` for session records.
- `/memory/eng/<engagement_id>/records/<memory_id>` reserved, unused until engagement identity exists.
- `/memory/inst/<topic>/records/<memory_id>` reserved for the institutional tier.

ADR-010 matches path only and ignores the query, and it declares query-identified routes not scopable. Therefore every route that selects a record must carry the scope in the **path**: a list or search route must be per scope (`/memory/agent/nova/records`), and a free-text `q` parameter may narrow but never select the scope. **What cannot be a path:** classification ceilings, purpose, consent and retention terms. These are record attributes checked by the service, not scopes. Identifiers must already be canonical (no encoded `/`, no dot segments).

## 5. PostgreSQL design questions for PR 1 (proposals)

- **Layout.** One database, one `memory` schema, scope columns (`owner_agent_id`, `memory_type`, `scope_key`) with composite indexes, **not** a schema per agent. Reason: ADR-009 makes the memory service the only holder of credentials, so per-agent database roles would not be exercised by agents; isolation is enforced by the service from the verified token. Row-level security driven by `SET LOCAL app.agent_id` is proposed as defense in depth, tested, but not the control.
- **Database roles.** One service role for reads and writes, one migration role. No agent holds a database credential.
- **Isolation and locking (proposal, to be fixed by tests).** `READ COMMITTED` with explicit row locks and conditional updates (`WHERE rev = $n AND lifecycle = 'active'`), per ADR-009's PostgreSQL mapping. The tests that decide it: writer versus erase, writer versus retire, two writers on one record (one loses on `rev`), duplicate idempotency key, create versus tombstone, and a policy change between read and write. If any interleaving fails, move that mutation to `SERIALIZABLE` with retry.
- **Compose service.** `postgres` on the private `agent-network`, no published port (or `127.0.0.1` only), a named volume, a healthcheck, credentials from environment and `.env` (never in source), and a `depends_on` health condition for the memory service. Image tag and resource use are **not verified** and must be pinned and checked when added.
- **Migrations.** Minimal: numbered plain `.sql` files and a small runner. **No PostgreSQL driver is in `requirements.txt` or `pyproject.toml`**, so PR 1 must add one deliberately and justify it (AGENTS.md prefers the standard library where practical; the standard library has no PostgreSQL client).
- **`remember()` transition.** Keep `remember()` and its JSONL as a non-authoritative trace. The memory service writes its own records. Do not delete or rewrite existing traces. Decide separately whether any past traces are ever imported (default: no).
- **Service shape.** A new FastAPI service speaking plain JSON REST, adopting the existing `service_auth` verifier, with the policy table as repository configuration.

### Test plan mapped to the #40 exit criteria and #48

| Exit criterion or #48 case | Test |
|---|---|
| A permitted agent writes and retrieves after restart | Write, restart the memory service and the database container, retrieve in a new session |
| Unpermitted agents, tenants and public users cannot retrieve or infer | Wrong agent, wrong scope, forged `sub`, missing token, and no information leak in error bodies or timing-shaped differences |
| Update, expiry and deletion take effect | Supersede, expire (read-time), retire and erase; erased and retired records are never returned; derived caches do not exist in PR 1 |
| Memory text cannot override system instructions or grant tools | A stored instruction-like payload is returned only wrapped as data with provenance and a "not instructions" marker, and no tool or permission changes |
| Concurrent writes and idempotency | The interleavings listed above |
| #48 poisoning and cross-agent isolation | Stale and tombstoned records, malicious instructions, cross-agent reads, restart persistence |
| No sensitive content in logs | Log capture shows identifiers only |

## 6. Recommended PR 1 scope, deferrals and founder questions

**PR 1 (governed storage and write lifecycle):** a backend-agnostic repository interface with a PostgreSQL adapter; the Compose `postgres` service; the memory service skeleton with deny-by-default policy table and A2 verification; **agent-private and session tiers only**; versions, idempotency, retire, erase with tombstones, and read-time retention; audit logging without content; the contract and interleaving tests that run offline against the local container.

**Explicitly deferred:** engagement-shared memory (no engagement identity), cross-scope promotion (ADR-009 denies it), the institutional tier's migration of the knowledge broker, semantic search, object storage for large bodies, any Stage or Production provisioning, wiring retrieval into agent execution (PR 2), and the broker's reconciliation with ADR-009 status words (flag now, resolve in PR 1 or 2).

**Open questions for the founder**
1. Accept ADR-009 as amended, so PR 1 builds against an accepted design?
2. Is agent-private plus session memory an acceptable first delivery, with engagement-shared memory waiting for an engagement identity?
3. Extend A2 to Research, Security, Sage and Pixel (#235) before PR 2, since they are the memory writers?
4. Stage plan and instance size for DigitalOcean Managed PostgreSQL, and backup retention (backups keep erased data).
5. Who is named in the engagement-closure policy (needed before engagement memory)?
6. Which PostgreSQL driver is acceptable as a new dependency?

## Corrections to other documents (not edited here)

- Issue #40 cites "ADR-003"; the design is ADR-009. The issue also lists eight named identities and "four bound identities"; the current binding count is in section 2.
- `live_stream._remember` is not memory; consider renaming it to avoid confusion.

## What was verified and what was not

**Verified in code:** every `remember()` call site and its line, the absence of any reader, `live_stream._remember` semantics, the Compose and DigitalOcean mounts, the six Stage services, the A2 verifier being installed only in `specialist_agent.py`, the knowledge module's models, grants, store and retrieval, that nothing outside tests imports it, and that no PostgreSQL driver is a dependency.

**Not verified:** test results (not run), the contents of any stored memory, Docker image tags or resource use, whether a suitable PostgreSQL driver is acceptable, DigitalOcean plan sizes and prices, and PostgreSQL locking behavior under each proposed interleaving (the conformance tests must prove it).

**Blockers for PR 1:** founder acceptance of ADR-009 and a decision on the PostgreSQL driver. Not blockers but constraints: A2 coverage (#235) and the missing engagement identity.

This document does not claim that #40 or any gate is complete.
