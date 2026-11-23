# Stage memory acceptance and release checklist

**Status: an operator checklist. Nothing in it has been run, and no row below is PASS.** Every status in this file is NOT RUN, NOT BUILT or OPEN unless the evidence column names a recorded artifact. Stage has no memory service, nothing is provisioned, no promotion has been requested, and issue #40 stays open. This document authorizes nothing: the only authorization to deploy is the founder's affirmative answer to the AGENTS.md question for one specific build.

This is an ordinary document under `docs/`. It collects, in one place, what an operator must show before and after a Stage memory window. It does not replace the procedure (`docs/stage-memory-runbook.md`, which owns the steps and gates G1 to G5), the design (`docs/stage-memory-design.md`) or the platform facts (`docs/stage-memory-platform-verification.md`). If this file disagrees with the code or the runbook, the code wins, then the runbook; fix this file.

Rules that hold throughout: the founder runs every DigitalOcean action; the assistant never runs `doctl`, never sees a secret, and never contacts DigitalOcean; no value (password, key, certificate body, CA text, database URL, address, app or cluster ID of the founder's account) is written into the repository, a pull request, an issue or a log excerpt. Record fixed codes, counts, fingerprint prefixes and timestamps only.

Status vocabulary: **PASS** (evidence attached and meets the condition), **FAIL**, **NOT RUN** (procedure exists, never executed), **NOT BUILT** (the thing to be tested does not exist on `main`), **OPEN** (a decision or tracker item without evidence).

## 1. Release checklist

Tick each line only with its evidence recorded in the window record (runbook section 8). A line without evidence is unticked.

State of `main` when this was written (baseline `a84f203`): launcher (PR #330), pinned certificate and material generator (PR #329), schema-skip fix (PR #328), list-audit `count` (PR #333), conformance fixture support (PR #334) and the retest kit (PR #331) are merged. **Not built:** the bootstrap entry (`MEMORY_STAGE_ROLE=bootstrap`; `src/viharbot/memory_service/stage_entry.py` accepts only `serve`), and the Stage app spec (`deploy/digitalocean/app.yaml` has the web and six agents only).

### 1.1 Code and build readiness

- [ ] The bootstrap entry (design item 6) is merged. Today: NOT BUILT. It waits for the probe retest.
- [ ] The Stage app spec (design item 11) is merged with the founder's explicit approval. Today: NOT BUILT.
- [ ] The commit to deploy is on `main`, its full SHA recorded; no unmerged branch is built or deployed.
- [ ] `python -m pytest -q` passes on that commit; record total, passed, skipped. Database tests skip without a PostgreSQL DSN; that skip is expected here and does not count as database evidence.
- [ ] CI on that commit is green (Python 3.12 and 3.13, frontend, lint, Docker build and smoke test, CI result). CI does not run PostgreSQL by decision, so a green CI is not conformance evidence (section 3 and section 9).
- [ ] No deployment trigger exists: `deploy_on_push` stays disabled; a push to `main` updates source only.

### 1.2 Platform and cost readiness

- [ ] Probe retest R1 to R3 executed once and results recorded in `docs/stage-memory-platform-verification.md`. Today: NOT RUN. No R1 to R3 results exist in that file or on issue #40.
- [ ] Database slug taken from `doctl databases options slugs --engine pg`; the price seen in the control panel estimate recorded (list price is UNCONFIRMED).
- [ ] Cost estimate (runbook section 3, item 4) recomputed with the price seen, and the window length agreed.
- [ ] The non-secret resource inventory (section 7) is open and empty, ready to fill as resources are created.

### 1.3 Gates (runbook section 2)

- [ ] G1 Materials prepared.
- [ ] G2 Conformance passed on this window's cluster, operator rule removed and the removal verified.
- [ ] G3 Promotion question answered "yes" for that commit.
- [ ] G4 Post-deploy verification passed (health, write/restart/read, failure drills).
- [ ] G5 Teardown verified (or the founder explicitly extends the window in writing).

### 1.4 Promotion summary (before asking the question)

The assistant posts the runbook 4.5 template: full commit SHA; offline suite, local rehearsal, retest R1 to R3 and conformance results each PASS or FAIL with date; the cost change (about $0.030 per hour while up, unconfirmed against an invoice); and the scope in plain words (Charlie only, five metadata fields, fail-open, empty at the start of the window). Then the literal question: **"Do you want to promote this dev build to Stage?"** Wait for an affirmative answer for that specific build. A different commit needs a new question.

## 2. Acceptance matrix

Evidence must be a recorded artifact (a result count with date and commit, a fixed-code log line, a command output summarized without secrets, an issue or PR link), never an assurance.

### 2.1 Issue #40 exit criteria

From the status comment on issue #40 ("Remaining before this can be marked complete"). Status as of baseline `a84f203`.

| # | Exit criterion (#40) | Evidence required | Status |
|---|---|---|---|
| E1 | Write, new session, retrieve, correct use for each operational binding | For Charlie: contract test `test_write_then_restart_then_retrieve_in_a_new_session` against PostgreSQL (section 3) and the Stage drill (section 4). For Nova, Scout, Cipher, Pixel and Sage: a wiring change per agent plus its own write, new session, retrieve evidence | Charlie local: run locally and recorded on #40 (Compose smoke test, local contract tests); Charlie on Stage: NOT RUN. Other agents: NOT BUILT (only Charlie is wired) |
| E2 | The #48 memory-poisoning and cross-agent isolation suite (wrong tenant, forged identity, malicious instructions, concurrent writes) | #48 closed by the founder with its suite merged and passing; for Stage, the same suite or the in-scope contract tests on the managed cluster | OPEN (#48 still open when #40's comment was written; confirm current state before relying on it). The contract file does cover forged sub, cross-agent partitions, wrapped instruction-like content and concurrent writers locally; that is not #48 |
| E3 | Per-tenant or engagement isolation | An engagement identity exists and grant-based authorization (#38) is built, with a test per boundary | NOT BUILT (no engagement identity; #38 not built) |
| E4 | Verification of memory-text handling with a real model, not only stubs | A founder-approved, opt-in live check showing wrapped memory text does not steer a real model. The assistant never makes this call implicitly (AGENTS.md) | NOT RUN; needs the founder's approval. Gateway state for the relevant alias is noted in project memory as broken; check before planning |
| E5 | A durable Stage backend: Managed PostgreSQL provisioned, Stage TLS and service-authentication key storage designed, contract suite run against a Stage database, Stage spec with the memory service | Gates G1 to G5 (section 2.2) with the window record; the contract suite counts from G2; the app spec merged | NOT RUN. Design exists (`docs/stage-memory-design.md`); launcher and generator merged; bootstrap and app spec NOT BUILT; nothing provisioned |
| E6 | Documented AWS and GCP storage and deployment gaps for Production | A reviewed document naming each gap per cloud | OPEN (no evidence on `main`; Production has no memory design) |

Not part of #40's exit and not tested here: downstream work #301, #302, #303, #305, and what Charlie should remember long term.

### 2.2 Runbook gates G1 to G5

Procedure and commands are in the runbook; the right-hand column is what this file requires as evidence.

| Gate | Runbook step | Concrete evidence required | Status |
|---|---|---|---|
| G1 Materials prepared | 4.3 | Generator run locally (`scripts/stage_memory_materials.py`); the file names present; certificate fingerprint (prefix) and expiry; confirmation the files sit in the password manager; no value anywhere else | NOT RUN |
| G2 Conformance passed | 4.4 | Counts (total, passed, failed, skipped), date, commit, cluster plan and PostgreSQL major version; the only skip is the container-restart test; firewall list showing no operator `ip_addr` rule after removal; the attempt to connect from the operator machine after removal refused (R1 says whether denial can be relied on) | NOT RUN (fixture support merged in PR #334; never run on a managed cluster) |
| G3 Promotion question answered | 4.5 | The posted summary (section 1.4) and the founder's literal "yes", recorded with the full commit SHA | NOT RUN (no promotion requested) |
| G4 Post-deploy verification passed | 6.1 to 6.4 | Newest deployment phase ACTIVE with its ID; bootstrap job success line; `memory` health 200 (or `create ... ok` line); drill table all PASS (section 4 and section 5 here); no content in any log | NOT RUN, and not runnable until the bootstrap entry and the app spec exist |
| G5 Teardown verified | 10 | The five clean commands summarized (apps, databases, firewalls, registry, balance); billing page after about an hour showing line items stopped; final cost against the estimate | NOT RUN |

### 2.3 Supporting checks the gates depend on

| Check | Evidence required | Status |
|---|---|---|
| Local rehearsal (design 7.2): launcher and bootstrap in Compose against a local PostgreSQL with a non-superuser admin stand-in, bootstrap run twice, write, restart, read, no secret in a log | Recorded run: date, commit, outcome per step | NOT RUN (the bootstrap does not exist) |
| Offline suite on the deploy commit | `python -m pytest -q` counts | NOT RUN for the future commit |
| Retest R1 to R3 (design 7.3) | Written results in the verification file | NOT RUN |
| Cluster CA differs per cluster (design 7.3) | Recorded yes or no after creating a second cluster or the window's cluster | UNVERIFIED |

## 3. PostgreSQL conformance procedure (gate G2)

Purpose: the same contract suite that passes against a local container must pass against the window's managed cluster (ADR-009 conformance), before the app exists. CI does not run PostgreSQL by decision (`docs/memory-service.md`, `docs/continuous-integration.md`), so this run is the evidence.

Fixture variables (from `tests/memory_helpers.py`):

| Variable | Use |
|---|---|
| `VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN` | Admin DSN. Unset: database tests skip. For the managed cluster use the admin role the platform gives (a non-superuser holding CREATEROLE and CREATEDB) with `sslmode=verify-full` and `sslrootcert=<path to the saved CA file>`. The derived service and migrator DSNs inherit both. |
| `VIHARBOT_MEMORY_TEST_PG_ALLOW_REMOTE=1` | Required because the host is not loopback. |
| `VIHARBOT_MEMORY_TEST_PG_CONFIRM_EMPTY=1` | Operator's statement the cluster is empty. The fixture then checks and refuses if any database exists beyond `postgres`, `template0`, `template1`, `defaultdb`, `_dodb` and the `vbtest_` prefix, or if roles `memory_migrator` or `memory_service` exist. |
| `VIHARBOT_MEMORY_TEST_PG_FORCE_REMOTE_GUARD=1` | Applies the remote rules to a loopback host. Use it in step 1 to exercise the guard locally. |
| `VIHARBOT_MEMORY_TEST_PG_CONTAINER` | Local Docker container name for the restart test only; leave unset on a managed cluster (that test skips). |

The run creates `vbtest_m_<hex>` and `vbtest_s_<hex>` roles and a `vbtest_<hex>` database and drops only those; it does not alter `memory_migrator`, `memory_service` or their database.

Steps (the founder's shell; the DSN, with its password, is typed into the environment of that shell and nowhere else):

1. **Local dry run of the guard.** With the local throwaway container, set `VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN` and `VIHARBOT_MEMORY_TEST_PG_FORCE_REMOTE_GUARD=1`, without the other two; expect a refusal. Add `ALLOW_REMOTE=1` and `CONFIRM_EMPTY=1`; expect the suite to run. Run `python -m pytest tests/test_memory_fixture_guard_unit.py -q` (offline).
2. **Baseline locally.** Run the full contract file against the local container with `VIHARBOT_MEMORY_TEST_PG_CONTAINER` set; record counts. This is the comparison.
3. **Add the temporary operator rule** (runbook 4.4 step 2) and record its UUID in the inventory (section 7).
4. **Run on the managed cluster:**

   ```text
   python -m pytest tests/test_memory_postgres_contract.py -q -ra
   ```

   with the three variables above set (admin DSN with `verify-full` and CA, `ALLOW_REMOTE=1`, `CONFIRM_EMPTY=1`). Use `-ra` so skip reasons are printed.
5. **Judge.** PASS: every in-scope test passes; the only skip is `test_records_survive_a_database_container_restart`. FAIL: any failure, any other skip, or a refusal from the empty-cluster check (a refusal means the cluster is not the window's fresh one; stop and investigate, do not bypass it).
6. **Remove the operator rule in the same session** and verify with `doctl databases firewalls list`; no `ip_addr` rule may remain.
7. **Confirm refusal** from the operator machine (retest R1 context).
8. **Record** the result per section 6 (counts, date, commit, plan, PostgreSQL major version; no connection detail).

Cost note: the database bills per started hour with a one-hour minimum; this run adds one to two billed hours.

## 4. Write, restart, read persistence verification (gate G4)

Two layers. Both must PASS.

**Layer A, database persistence, local only.** `test_write_then_restart_then_retrieve_in_a_new_session` runs on any PostgreSQL. `test_records_survive_a_database_container_restart` needs a local Docker container and is skipped on a managed cluster; it is the local evidence that rows survive a database restart. Evidence: counts from the local run (step 2 of section 3).

**Layer B, Stage drill with Charlie (runbook 6.3).** Preconditions: the app spec and bootstrap exist and G3 is answered.

1. Send one synthesis request through the normal route. Evidence: a memory log line with `operation` `create`, `outcome` `ok`, `scope` `agent/charlie`; and Charlie's `memory client` line with `decision` `ok`. Record the timestamps and request IDs (identifiers only).
2. Record the deployment ID, then restart every component (`doctl apps create-deployment <app-id>`; wait for ACTIVE). Record the new deployment ID. A restart of the app does not restart the managed database, so this proves the app path and rehydration, not database-level durability on Stage; do not claim the latter. A managed-database restart is not driven from here (the Stage drill covers the application only).
3. Send a second request. Evidence: a memory line `operation` `list`, `outcome` `ok`, `count` of at least 1, and Charlie `decision` `ok`.
4. PASS: all lines present, no `denied` or `error`, no prompt, answer, token or request body in any log. FAIL: any missing line, any content in a log (a defect to report, not to work around).

## 5. Failure and recovery drills (gate G4)

One at a time, with drill materials from a separate generator run (`--out data/stage-memory-drill`) and never from the live directory. Between drills restore the correct value and redeploy. In every drill Charlie must still answer, within its time bound. Procedure: runbook 6.4.

| Drill | Induce | PASS evidence | Recovery proof | Status |
|---|---|---|---|---|
| Memory down | Wrong `MEMORY_DB_PASSWORD` on `memory`, redeploy | Charlie answers; one content-free `memory client` line with `reason` `http_503` (or `unreachable`, `timeout`); `/health` 503 | Restore the value, redeploy, `/health` 200, 6.3 passes | NOT RUN |
| Wrong certificate | Drill `memory.crt.b64` into Charlie's `VIHARBOT_MEMORY_CA_PEM` | Answers; `reason` `tls_verification_failed`; no request sent | Restore, redeploy `synthesis`, 6.3 passes | NOT RUN |
| Expired certificate | A certificate with a past `notAfter`; the generator's `--days` accepts 1 to 3650 only, so this needs a one-off method (runbook 6.4 note) | Same as wrong certificate | Same | NOT RUN or SKIPPED (record which) |
| Wrong signing key | Drill `charlie-signing.key` into Charlie's `VIHARBOT_SIGNING_KEY` | Answers; memory audit `denied` with `reason` `authentication_failed`; Charlie `reason` `denied`; no record written by the wrong key | Restore, redeploy, 6.3 passes | NOT RUN |
| Database unreachable | Remove the app's trusted-source rule (`firewalls remove --uuid`) | `/health` 503; Charlie answers with `http_503` or `timeout` | `append --rule app:<app-id>`, `/health` 200 | NOT RUN |
| Bootstrap job fails | Per runbook 11: deliberately fail the job once | Deployment ERROR, old version keeps serving, rollback follows; the number of runs per failed deployment recorded (retest R3) | Correct the cause, redeploy; second run is idempotent | NOT RUN, and NOT BUILT (no bootstrap) |

After the drills restore the live values, redeploy, and re-run 6.3 once.

## 6. Window record and results

Use the runbook section 8 template for the window record. Add the following rows so each criterion above has its evidence in one place. Keep it in a private note; an issue comment on #40 is acceptable if the founder chooses, and it must not claim a gate or the issue complete.

```text
Gates:     G1 ___  G2 ___  G3 ___ (answered at ___)  G4 ___  G5 ___
Conformance: total ___ passed ___ failed ___ skipped ___ (expected: 1, the container-restart test)
Exit criteria evidence: E1 ___  E2 ___  E3 ___  E4 ___  E5 ___  E6 ___
Drills:    memory down ___  wrong cert ___  expired ___  wrong key ___  db unreachable ___  bootstrap fail ___
Persistence: create ok at ___  deployment before ___  after ___  list ok count ___
Surprises: ______
```

## 7. Non-secret resource inventory and cost tracking

Fill as each resource is created. Allowed: kind, a short label, creation and deletion timestamps (UTC), who created it, billing basis. **Not allowed:** passwords, tokens, keys, certificate or CA text, connection strings, IP or CIDR values, and the founder's app and cluster IDs (use the label, for example `cluster-A`, `app-stage`, and keep the real IDs in the founder's private note).

| Label | Kind | Slug or size | Created (UTC) | Deleted (UTC) | Billing basis | Verified gone (command, date) |
|---|---|---|---|---|---|---|
| cluster-A | Managed PostgreSQL (single node, Standard) | ___ | ___ | ___ | per started hour, 1-hour minimum | `doctl databases list` ___ |
| db-A | Dedicated database inside cluster-A | n/a | ___ | gone with cluster | included | n/a |
| rule-operator | Trusted-source rule (operator, temporary) | n/a | ___ | ___ | none | `firewalls list` ___ |
| rule-app | Trusted-source rule (app) | n/a | ___ | ___ | none | `firewalls list` ___ |
| memory | App component (service) | `apps-s-1vcpu-0.5gb` (UNCONFIRMED) | ___ | ___ | per component hour | `apps list` ___ |
| memory-bootstrap | App component (pre-deploy job) | ___ | ___ | ___ | only while it runs | `apps list` ___ |
| deployments | Deployment IDs (labels `d1`, `d2`, ...) | n/a | ___ | n/a | none | n/a |
| local files | `data/stage-memory/`, `data/stage-spec-filled.yaml`, `data/stage-memory-drill/` | n/a | ___ | ___ | none | `ls data` ___ |

Cost tracking (runbook estimate: about $0.030 per hour; database $0.02254, one extra memory component $0.00744; both UNCONFIRMED until an invoice):

| Item | Estimate | Price seen at creation (source) | Hours up | Actual from invoice | Difference |
|---|---|---|---|---|---|
| Database | $___ | ___ | ___ | $___ | $___ |
| Memory component | $___ | ___ | ___ | $___ | $___ |
| Conformance run (1 to 2 billed hours) | $___ | ___ | ___ | $___ | $___ |
| Total | $___ | | | $___ | $___ |

Per-window estimate (runbook table): 24 hours $0.72, 3 days $2.16, 7 days $5.04, forgotten for 28 days $20.15. Note "balance get" month-to-date at creation, at close and after about an hour post-teardown. If the invoice disagrees, record the invoice and correct the prices in the runbook.

## 8. Rollback, teardown and final acceptance

### 8.1 Rollback (go back to the pre-window Stage)

Triggers: any FAIL in G4; any content-like text in a log; the founder's instruction; an unexpected cost growth. Rollback never switches a connection to `require`, disables hostname verification or improvises around a security check.

1. **Charlie off memory without touching the other agents:** remove Charlie's memory variables (above all `VIHARBOT_MEMORY_URL`, since memory is on whenever it is set) in the founder's local spec copy, apply, wait for ACTIVE; Charlie then logs no memory call. (Runbook 10, option B.)
2. **Previous app deployment:** a failed bootstrap already triggers an automated rollback that finishes in seconds (TESTED). For a bad but healthy deployment, redeploy the previous known-good commit's spec.
3. Re-run the read-only checks: `doctl apps list-deployments` shows ACTIVE and Charlie answers a request.
4. Record the rollback in the window record; do not call the build verified.

### 8.2 Teardown (runbook section 10, gate G5)

Order: app (or the memory pieces), then trusted-source rules, then the cluster, then local files. Destroying the cluster destroys its backups; memory starts empty each window.

1. Choose option A (delete the whole `viharbot-stage` app, only if all of Stage goes off) or option B (remove only the memory component, job and binding, keep the web and six agents).
2. List and remove every firewall rule, including the operator rule; delete the cluster; delete `data/stage-spec-filled.yaml` and `data/stage-memory-drill`. Keep `data/stage-memory/` only if the founder keeps the yearly certificate, signing key and registry; always delete the database passwords and `db-ca.pem`.
3. Verify nothing remains: `doctl apps list`, `doctl databases list`, `doctl databases firewalls list <cluster-id>` (should error), `doctl registry get` (none), `doctl balance get`. Control panel billing after about an hour must show stopped line items.
4. Fill the inventory's "verified gone" column and the final cost.

### 8.3 Final acceptance criteria

The window is accepted, as a window, only when all of these hold and are recorded:

- G1 to G5 each PASS with evidence (section 2.2).
- Conformance PASS with the only skip the container-restart test (section 3).
- Write, restart, read PASS and every failure drill PASS or explicitly SKIPPED with the founder's written acceptance (sections 4 and 5).
- Teardown verified: no Stage-memory resource listed, billing stopped, final cost recorded against the estimate with the invoice winning any disagreement.
- No secret, value or founder ID in any repository file, pull request, issue or pasted log.

**Accepting a window does not close issue #40.** #40 closes only when the founder decides the exit criteria E1 to E6 are met; E1 for the other agents, E2, E3, E4 and E6 are independent of any Stage window and are NOT RUN, NOT BUILT or OPEN today. Never write a closing keyword before an issue number in a pull request; use "Part of #40".

## 9. CI coverage for this work

Read from `.github/workflows/ci.yml` and `docs/continuous-integration.md` at baseline `a84f203`. This file does not change `.github/`.

| Area | Covered by CI | Gap |
|---|---|---|
| Memory unit, replay, launcher, certificate pin, generator, schema-skip and fixture-guard tests (`tests/test_memory_*`, `tests/test_stage_memory_materials.py`) | Yes, in the offline suite on Python 3.12 and 3.13 | none |
| PostgreSQL contract tests (`tests/test_memory_postgres_contract.py`) | No: skipped, by founder decision | G2 is the only PostgreSQL evidence; a regression in the SQL layer is not caught on a pull request. The skip is allow-listed in the Docker test run |
| Container-restart durability test | No | Local only (needs Docker) |
| `scripts/` and `deploy/` changes | Docker job pattern does not include `scripts/` or `deploy/` (`pattern` in `ci.yml`), so a pull request changing only those skips the Docker job; the host jobs still run | A broken `deploy/digitalocean/app.yaml` or Dockerfile.memory path would be found only by the push to `main` run |
| App spec validity | `tests/test_memory_deployment_static.py` runs offline; `doctl apps spec validate` is founder-run | No offline schema validation against DigitalOcean's real spec schema |
| Stage deployment, health and drills | No, by design: CI never deploys | All of sections 4 and 5 are manual |
| Documentation | `tests/test_docs_consistency.py` runs in the suite | It cannot catch stale status prose such as the runbook section 9 drift this file's companion pull request fixed |

## 10. Known limitations of this file

- It is a checklist written before the bootstrap entry and the app spec exist; steps that depend on them are marked and may change with the code.
- Prices are list prices, not invoices.
- It says nothing about Production, AWS or GCP.
- Exit criteria E1 to E6 are taken from the status comment on issue #40; re-read the issue before relying on them.
