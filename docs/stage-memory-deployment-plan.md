# Stage memory deployment plan (first deployment done 2026-11-24)

**Status: the plan was executed once. On 2026-11-24 the app spec described here (`deploy/digitalocean/app.yaml`, design item 11, merged) was deployed to Stage from `main` commit `3bfc6c6`, after the founder answered the promotion question yes for that commit. A first 3-day window is running (about 2026-11-24 06:24 UTC to about 2026-11-27 06:30 UTC). Nothing is torn down. Issue #40 stays open.** The only authorization to deploy is the founder's affirmative answer to "Do you want to promote this dev build to Stage?" for one specific commit (`AGENTS.md`); a different commit needs a new question. The sections below are kept as written, with "What the first deployment actually did" added after section 2 and the open questions updated in section 7. Where an older sentence says "none exists today" or "proposed", it describes the plan before 2026-11-24. This is an operator document under `docs/`, not an ADR and not a governance document. If it disagrees with the code or the spec file, the code wins; fix this document.

It does not repeat the step-by-step window procedure. That is `docs/stage-memory-runbook.md`; the design is `docs/stage-memory-design.md`; platform observations are `docs/stage-memory-platform-verification.md`. This file adds one thing those do not hold in one place: a reproducible manifest of what the proposed spec creates, what must exist in DigitalOcean first, the cost, and how to undo it.

**Labels.** TESTED = observed by the founder's probe on 2026-11-12 or the probe retest on 2026-11-23 (one account, one cluster each time; the retest ran on Standard PostgreSQL 17.12, slug `db-amd-1vcpu-1gb`, region nyc / nyc1, as `doadmin`). DOCUMENTED = a DigitalOcean page says it, as recorded in the verification file. UNVERIFIED = nobody has checked it. UNCONFIRMED (for prices) = a list price read from a page, never matched to an invoice.

## 1. Dependencies and what is held

| Item | State | Why it matters here |
|---|---|---|
| Bootstrap job (`MEMORY_STAGE_ROLE=bootstrap`) | merged (#354, with the `ALTER ROLE` fix in PR #361); ran on Stage on 2026-11-24 | The spec's `memory-bootstrap` job runs this code. The migrator CONNECT grant (required) and the `WITH SET TRUE` grant (accepted, optional) are TESTED on DigitalOcean (probe retest 2026-11-23). Client-side SCRAM hashing in it worked on DigitalOcean on 2026-11-24 (the plain-password fallback stays in the code). |
| Launcher (`serve`), Charlie's certificate variable, material generator, `verify-full` support, migration runner skip, conformance fixtures | per runbook section 9 | Must be on `main` before any window. Re-check against `main`. |
| Probe retest R1 to R3 | ran 2026-11-23, cluster torn down; results in section 7 | Answered the bootstrap SQL questions and showed the trusted-source rule refuses public and sibling-app clients. The private-VPC path from an app attached to the VPC is still untested. |

This spec file changes nothing at runtime until the founder applies a filled local copy. `deploy_on_push` is `false` on every component, including the new job.

## 2. Reproducible manifest

The proposed `deploy/digitalocean/app.yaml` (read it; this table is a summary) adds one database binding, one service, one job and six variables on `synthesis`. The existing `web` and five other agents are byte-for-byte unchanged, and a static test pins their variable names (`tests/test_memory_deployment_static.py`).

**Fill in locally, never in the repository:** `PLACEHOLDER_CLUSTER_NAME` and `PLACEHOLDER_DATABASE_NAME` in the `memory-db` binding, and every `SECRET` value (all empty in the file). The filled copy lives at `data/stage-spec-filled.yaml` (gitignored).

**Build identity to record for a window:** full commit SHA ______ ; image builds from branch `main` of `vchokshi/viharbot` using `Dockerfile.memory` (one image, role chosen by `MEMORY_STAGE_ROLE`); base `python:3.12-slim-bookworm`; driver `psycopg[binary]==3.3.8` (the `memory` extra).

| Component | Kind | Port and route | Size | Role variable | Reads (variable: type) |
|---|---|---|---|---|---|
| `memory` | service | `internal_ports: [8443]`, **no `http_port`, no route**, TCP health check on 8443, one instance | `apps-s-1vcpu-0.5gb` | `MEMORY_STAGE_ROLE=serve` | `PORT=8443`: general; `MEMORY_TLS_CERT_PEM`, `MEMORY_TLS_KEY_PEM`, `VIHARBOT_KEY_REGISTRY_B64`, `MEMORY_DB_PASSWORD`, `MEMORY_DB_CA_PEM`: secret, empty in file; `MEMORY_DB_HOST`, `MEMORY_DB_PORT`, `MEMORY_DB_NAME`: binding references; `MEMORY_DB_USER=memory_service`, `MEMORY_DB_SSLMODE=verify-full`: general |
| `memory-bootstrap` | job, `PRE_DEPLOY` | none (a job cannot receive requests, DOCUMENTED) | platform default (billed only while it runs, DOCUMENTED row 8d) | `MEMORY_STAGE_ROLE=bootstrap` | `MEMORY_ADMIN_DB_HOST`, `_PORT`, `_USER`, `_NAME`: binding references; `MEMORY_ADMIN_DB_PASSWORD`: secret, binding reference; `MEMORY_ADMIN_DB_SSLMODE=verify-full`; `MEMORY_DB_MIGRATOR_PASSWORD`, `MEMORY_DB_SERVICE_PASSWORD`, `MEMORY_DB_CA_PEM`: secret, empty in file |
| `synthesis` (Charlie) | existing service, gains variables only | unchanged (`internal_ports: [8000]`) | unchanged | n/a | adds `VIHARBOT_MEMORY_URL=https://memory:8443`, `VIHARBOT_OUTBOUND_AUTH=on`, `VIHARBOT_SIGNING_KID`, `VIHARBOT_SERVICE_SUBJECT=charlie`: general; `VIHARBOT_MEMORY_CA_PEM`, `VIHARBOT_SIGNING_KEY`: secret, empty in file |
| `web`, `research`, `security`, `fact-check`, `source-quality`, `privacy-review` | existing | unchanged | unchanged | n/a | no memory variable (tested) |
| `memory-db` | database binding (production cluster, `doadmin`) | n/a | n/a | n/a | exposes `HOSTNAME`, `PORT`, `USERNAME` (TESTED), `PASSWORD`, `DATABASE` (DOCUMENTED only) |

**Value provenance** (all generated by the founder locally, exactly as runbook 4.3; no assistant sees them): certificate and its key, Charlie's signing key and the registry from `scripts/stage_memory_materials.py`; the two role passwords from the same script (`MEMORY_DB_PASSWORD` and `MEMORY_DB_SERVICE_PASSWORD` must be the same value); the cluster CA from `doctl databases get-ca` or the control panel. `VIHARBOT_SIGNING_KID` in the file is the generator's default (`charlie-stage-1`); it must equal the key id the generator prints. Secrets are visible to anyone with console access to the app (design section 3); that is accepted for a lab window, not for sensitive data.

**Not in the spec, by design:** the managed cluster, the trusted-source rule, and the dedicated database. They are created by the founder with `doctl` (runbook 4.1, 4.6). Creating an app with empty `SECRET` values and the platform encrypting on first submission were **not tested** (verification row 7a); the runbook's local filled copy avoids relying on that. Whether a Dockerfile component honours `run_command` is UNVERIFIED and not used.

**Spec checks the repository can make offline** (`tests/test_memory_deployment_static.py`): no memory variable on `web` or the five other agents; only `synthesis` carries the client settings; `memory` has no `http_port`, no route and no top-level `ingress`; port 8443 only, `PORT=8443`, TCP check; the job is `PRE_DEPLOY` with no port; every `SECRET` is empty or a binding reference and no secret-named variable is plain; `deploy_on_push` is `false` everywhere; every memory variable in the spec is one the code reads. These prove the file's shape. They do not prove the platform accepts it.

### 2.1 Per-component LiteLLM keys and the web planner (founder decision 2026-11-23)

Each model-using component gets its own gateway key, created in LiteLLM as `stage-<component>` and restricted to that component's model alias (research `gpt-6.1-sol`, security `openai/gpt-5.4`, fact-check and source-quality `gemini/gemini-3.5-flash`, privacy-review and synthesis `openai/gpt-5.4-mini`, web `gpt-6-luna`). The spec carries only an empty `LITELLM_API_KEY` SECRET slot per component; the operator fills them in the local copy (the keys are held in gitignored files, never in this repository) and the platform encrypts them on first submission. Revoke a key by alias with the gateway's management key.

The `web` component now also sets `CHARLIE_PLANNER_MODEL: gpt-6-luna` and has a `LITELLM_API_KEY` slot. The live team conversation runs only when both are non-empty (`src/viharbot/live_team.py`), so with the `stage-web` key filled the web container makes Charlie's planner model calls itself; before this change Stage web held no key and ran the fixed pipeline. This adds model-call cost on Stage and puts a gateway key in the browser-facing container (scoped to one alias, no budget set). Two tests that said "the cloud spec never carries the live team's inputs" were narrowed to allow exactly this: the planner alias once, in `web`, and an empty SECRET slot, never a value.

## 2.2 What the first deployment actually did (2026-11-24)

Facts observed by running it (one account, one day); the details and results are in `docs/stage-memory-platform-verification.md` ("First Stage window 2026-11-24") and `docs/stage-memory-runbook.md` (4.6).

- **How it was applied.** The app `viharbot-stage` (web, six agents, the memory service and the `PRE_DEPLOY` memory-bootstrap job) was **created** from a locally filled copy of the spec, with the secrets pasted into a gitignored local file; the platform encrypted the SECRET values on first submission, which worked with values supplied at creation. (The plan's earlier note that creating with empty SECRET values is untested still stands.) The Stage managed PostgreSQL 17 cluster (single node, slug `db-amd-1vcpu-1gb`, region `nyc1`, default VPC) is in the founder's `viharbot` DigitalOcean project on the My Team team. `doctl apps create` put the app in the default project; it was assigned to `viharbot` afterwards.
- **Trusted source.** The first deployment's job succeeded while the cluster had no trusted source (an empty list means no restriction). The app was then added as the cluster's only trusted source and redeployed once; all fine.
- **Bootstrap.** First run on an empty cluster: 12 steps ok with `verify-full`, client-side SCRAM role creation, grants, schema, migrations 0001 and 0002, service login. Second run: all 12 ok, `alter_role` steps ok, migrate applied nothing. The bindable names `HOSTNAME`, `PORT`, `USERNAME` and `DATABASE` resolved and worked. A local rehearsal had first found that `ALTER ROLE` naming `SUPERUSER` fails for a non-superuser admin on PostgreSQL 16 and later; PR #361 fixed it before the deployment, which matters because a rollback re-runs the job.
- **Incidents.** The first cluster was deleted in the console by the founder (about 06:17 to 06:20 UTC) while the app ran; Charlie degraded fail-open as designed; the cluster was rebuilt (about 06:24 UTC), its CA was byte-identical, the trusted-source rule was added before anything connected, and the app was redeployed. Conformance was not re-run on the rebuilt cluster. A later full-spec `doctl apps update` reverted a console edit to a non-secret variable (a full-spec update replaces the whole spec; secret differences are masked in a diff).
- **Spec as deployed versus the plan.** The web component sets `CHARLIE_PLANNER_MODEL` and holds a gateway key (section 2.1), so Stage `/api/chat` uses the iterative live team and Charlie's memory is exercised only when the planner schedules synthesis. Seven per-component gateway keys exist; there is no budget or expiry on them. The spec sets no instance size, so the default applies (assumed $5, unconfirmed).
- **Cost.** About $0.082 per hour for 8 services at the assumed default size plus about $0.0225 per hour for the database, plus model-call cost; not read from an invoice.

## 3. Required DigitalOcean resources

Everything below is the founder's to create (runbook section 4). As of 2026-11-24 the cluster, the dedicated database, the app and the app's trusted-source rule exist for the first window (labels in `docs/stage-memory-acceptance.md` section 7; no IDs here).

| Resource | Needed | Notes |
|---|---|---|
| Managed PostgreSQL cluster | one, PostgreSQL 17, Standard Edition, single node, no standby | Slug chosen from `doctl databases options slugs --engine pg`; the probe used `db-amd-1vcpu-1gb` (15 GiB) and its price is UNCONFIRMED. Created per window (decision N1). In the VPC, in the datacenter the app region (`nyc`) maps to (TESTED). |
| Dedicated database | one, inside that cluster | Owned by `doadmin`, created by the platform step, not by the bootstrap. |
| Cluster CA | one per cluster | Public; pasted into `MEMORY_DB_CA_PEM` on `memory` and `memory-bootstrap`. A rebuilt cluster on this account had a byte-identical CA (the SHA-256 matched; observed twice on one account), so do not assume it changes; compare the hash. |
| Trusted-source rule for the app | one, `app:<app-id>` | Added by the founder immediately after the app exists (`doctl databases firewalls append`, TESTED). The first deployment's job may fail until it exists (runbook 4.6). Retest R1 (2026-11-23) showed a rule allowing only the main app refused a public laptop and an untrusted sibling app within about 40 s (TCP timeout) while the trusted app still connected: TESTED for the public path. The private-VPC path from an app attached to the VPC is UNTESTED. |
| Temporary operator trusted-source rule | optional, for the conformance run only | `ip_addr` form works (observed 2026-11-24); removed in the same session. The address is never written to the repository. |
| App Platform app | `viharbot-stage`, created from a filled local spec on 2026-11-24 | Eight services plus one job (web, six agents, memory; the job is the bootstrap). |
| Container registry, VPC, project, extra domains | **none** | None should be created. |

## 4. Cost estimate (UNCONFIRMED)

Sources: the runbook's section 3 item 4 and design section 9, which cite list prices recorded in `docs/stage-memory-platform-verification.md` (rows 3a, 3c, 8b, 8d), retrieved 2026-11-11. **No figure comes from an invoice.** The database price is for a "Standard 1 GiB" plan whose slug could not be matched to `db-amd-1vcpu-1gb` (row 3a); the component figure is the "extra $5-per-month-class component" (row 8b). Arithmetic: database 15.15 / 672 = $0.02254 per hour; memory component 5.00 / 672 = $0.00744 per hour; bootstrap job billed only while running, about 0; total about **$0.030 per hour**. The existing web and six agents are not counted.

| Window | Memory component | Database | Total |
|---|---|---|---|
| 24 hours | $0.18 | $0.54 | $0.72 |
| 3 days (72 h) | $0.54 | $1.62 | $2.16 |
| 7 days (168 h) | $1.25 | $3.79 | $5.04 |
| forgotten for 28 days (672 h) | $5.00 | $15.15 | $20.15 |

The database bills per started hour with a one-hour minimum (DOCUMENTED, row 3c); a conformance run before the app adds one to two such hours (about $0.02 to $0.05). Build minutes, bandwidth, storage beyond the plan and the 28-day billing wording in row 8b are UNVERIFIED. **If the control panel estimate or the invoice disagrees, they win.** The first invoice must confirm the database slug price, per-started-hour billing, that the job billed near zero, and that nothing else appeared (runbook section 8).

## 5. Rollback plan

Rollback here means returning Stage to "web plus six agents, no memory", or to the previous good deployment. It is the founder's to run; each command is one explicit instruction.

| Situation | Action | Effect | Evidence |
|---|---|---|---|
| The bootstrap job fails | do nothing; fix the cause and redeploy | A failing `PRE_DEPLOY` job blocks the deployment; the old version keeps serving; the platform starts an automated rollback within seconds (TESTED, probe 2026-11-12) | `doctl apps list-deployments <app-id>` shows ERROR then the rollback |
| The platform re-runs the job on rollback | none needed | The job is idempotent: roles are created or altered, grants and schema statements repeat safely, and migrations are checksummed and locked. A second run changes nothing. TESTED (probe retest 2026-11-23): a failing PRE_DEPLOY job ran once with no platform retry (deployment ERROR), and the automated rollback re-ran the previous version's job once about 20 s later; a second manual run exited 0 with the same outcomes. | job log lines for `alter_role_*` instead of `create_role_*` on the second run |
| Memory misbehaves but Stage should stay up | remove `VIHARBOT_MEMORY_URL` from `synthesis` in the filled copy and apply | Charlie stops calling memory immediately on the next deployment; there is no feature flag, memory is on whenever the URL is set | Charlie logs no `memory client` line |
| Back out the whole change | apply the previous spec (the current `main` spec without the memory pieces) with `doctl apps update <app-id> --spec <file>`, wait for ACTIVE | Removes `memory`, `memory-bootstrap`, the binding and Charlie's variables; web and the six agents are untouched | `doctl apps get <app-id>` |
| Back out the repository change | revert the spec pull request on `main` | No runtime effect on its own: nothing deploys on push | none needed |

Rollback does not touch the cluster. The cluster and its data go in teardown (section 6). The database role passwords and certificate are not recoverable from the app after removal; regenerate them next window (runbook 4.3).

## 6. Teardown requirements

Per decision N1 the cluster is destroyed every window, and destroying it destroys its backups (DOCUMENTED, row 3f). Order: app or memory pieces, then trusted-source rules, then the cluster, then local files. The commands and the nothing-left-billing checks are runbook section 10; this is the requirement list, which a window is not finished without.

1. Remove the memory pieces from the app (option B), or delete the whole `viharbot-stage` app (option A, which also removes the other seven components; the founder chooses).
2. List and remove every trusted-source rule on the cluster, including any operator-address rule. Record rule UUIDs in the window record.
3. Delete the cluster. Confirm `doctl databases list` shows no cluster for the window and `doctl databases firewalls list <cluster-id>` errors not found.
4. Confirm no container registry, VPC, project or domain was added (`doctl registry get` errors or shows none).
5. Delete the local filled spec and any drill materials; delete the database passwords and saved CA, which belong to a cluster that no longer exists. Keep yearly certificate material only if the founder keeps it.
6. Check billing after about an hour: the cluster and memory component line items must stop growing. Record the final cost beside the estimate.

A window is not closed until all six are done and recorded (gate G5).

## 7. Open questions for the founder

0. **Answered by the first deployment (2026-11-24).** The bindable names `HOSTNAME`, `PORT`, `USERNAME` and `DATABASE` resolved and worked; `db_user: doadmin` with an attached production cluster was accepted; client-side SCRAM hashing works on DigitalOcean; the app was created with filled SECRET values and encrypted on first submission; the `ip_addr` trusted-source form works; an empty trusted-source list is unrestricted; a rebuilt cluster had an identical CA. Still untested: creating with empty SECRET values and the private-VPC denial path. Open: the instance size (default, unconfirmed), the invoice price, three failure drills not yet run (wrong certificate, wrong signing key, expired certificate), conformance on the rebuilt cluster, and deterministic memory use under the live team.
1. **Retest R1 to R3 results: ANSWERED 2026-11-23** (Standard PostgreSQL 17.12, nyc1, `doadmin`; torn down). The migrator cannot log in to the platform-created database (42501) until `GRANT CONNECT ON DATABASE ... TO migrator` as `doadmin`; after it, login works, so the bootstrap's fatal CONNECT step is validated. `ALTER DATABASE ... OWNER TO migrator` fails (42501) without a membership grant and works after `GRANT migrator TO CURRENT_USER WITH SET TRUE` (accepted); the bootstrap does not change the owner, so that step stays optional and non-fatal. `CREATE SCHEMA IF NOT EXISTS` on the existing schema fails for the migrator, so the migration runner's existence check (#328) is necessary; check-then-skip works. A failing PRE_DEPLOY job is not retried but the rollback re-runs the previous version's job once, so the job must stay idempotent. A trusted-source rule refused a public laptop and an untrusted sibling app while the trusted app connected. **Still untested:** client-side SCRAM password hashing (plain fallback kept), the private-VPC path from an app attached to the VPC. PR #354 is awaiting founder review rather than held.
2. **`${memory-db.PASSWORD}` and `${memory-db.DATABASE}`**: `DATABASE` is observed working (2026-11-24; now covered by a test) and `PASSWORD` is used as a binding reference by the job. Creating an app with empty `SECRET` values is still untested.
3. **Binding shape.** Answered 2026-11-24: `db_user: doadmin` for an attached production cluster was accepted in the real `viharbot-stage` spec.
4. **Job size.** The job has no `instance_size_slug`, so the platform default applies. Say if a specific size is wanted.
5. **Spec shape for the first apply.** Add the memory pieces to the existing app (this proposal), or create a separate memory app for the window? The design chose the existing app; a separate app would keep Stage untouched and simplify teardown, at the cost of cross-app reachability (a second app could not reach the service in the probe, verification row 2c).
6. **`AGENTS.md`** says "Stage currently has the web service and six agents". It is now stale (Stage also runs the memory service and the bootstrap job); it is a founder-reviewed path and this change does not edit it (exact sentence in `docs/stage-memory-acceptance.md` section 11).
7. **Promotion.** This plan is not a request to deploy. The first promotion was answered yes for `3bfc6c6`; any other commit needs the runbook 4.5 summary and the literal question again.
