# Stage memory deployment plan (proposal, NOT approved for deploy)

**Status: a plan for the founder to review. Nothing is provisioned, nothing is deployed, no Stage promotion has been requested, and issue #40 stays open.** The app spec change this document describes (`deploy/digitalocean/app.yaml`, design item 11) is a proposal. The only authorization to deploy is the founder's affirmative answer to "Do you want to promote this dev build to Stage?" for one specific commit (`AGENTS.md`). This is an operator document under `docs/`, not an ADR and not a governance document. If it disagrees with the code or the spec file, the code wins; fix this document.

It does not repeat the step-by-step window procedure. That is `docs/stage-memory-runbook.md`; the design is `docs/stage-memory-design.md`; platform observations are `docs/stage-memory-platform-verification.md`. This file adds one thing those do not hold in one place: a reproducible manifest of what the proposed spec creates, what must exist in DigitalOcean first, the cost, and how to undo it.

**Labels.** TESTED = observed by the founder's probe on 2026-11-12 (one account, one day). DOCUMENTED = a DigitalOcean page says it, as recorded in the verification file. UNVERIFIED = nobody has checked it. UNCONFIRMED (for prices) = a list price read from a page, never matched to an invoice.

## 1. Dependencies and what is held

| Item | State | Why it matters here |
|---|---|---|
| Bootstrap job (`MEMORY_STAGE_ROLE=bootstrap`) | draft PR #354, **merge held until probe retest R1 to R3 is recorded** | The spec's `memory-bootstrap` job runs this code. The migrator CONNECT grant and the `WITH SET TRUE` grant in it are UNTESTED on DigitalOcean. If the retest changes that SQL, the job changes and this plan's job row stays as is. |
| Launcher (`serve`), Charlie's certificate variable, material generator, `verify-full` support, migration runner skip, conformance fixtures | per runbook section 9 | Must be on `main` before any window. Re-check against `main`. |
| Probe retest R1 to R3 | being run by the founder; results not recorded | Decides the bootstrap SQL and whether "the trusted-source rule refuses other clients" may ever be claimed. |

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

## 3. Required DigitalOcean resources

Everything below is the founder's to create (runbook section 4). None exists today.

| Resource | Needed | Notes |
|---|---|---|
| Managed PostgreSQL cluster | one, PostgreSQL 17, Standard Edition, single node, no standby | Slug chosen from `doctl databases options slugs --engine pg`; the probe used `db-amd-1vcpu-1gb` (15 GiB) and its price is UNCONFIRMED. Created per window (decision N1). In the VPC, in the datacenter the app region (`nyc`) maps to (TESTED). |
| Dedicated database | one, inside that cluster | Owned by `doadmin`, created by the platform step, not by the bootstrap. |
| Cluster CA | one per cluster | Public; pasted into `MEMORY_DB_CA_PEM` on `memory` and `memory-bootstrap`. Assume a new cluster has a new CA (UNVERIFIED). |
| Trusted-source rule for the app | one, `app:<app-id>` | Added by the founder immediately after the app exists (`doctl databases firewalls append`, TESTED). The first deployment's job may fail until it exists (runbook 4.6). That the rule refuses other clients is UNTESTED until retest R1 is recorded; do not claim it. |
| Temporary operator trusted-source rule | optional, for the conformance run only | `ip_addr` form UNVERIFIED; removed in the same session. The address is never written to the repository. |
| App Platform app | the existing `viharbot-stage` app, updated; or recreated | Today seven components. After: eight services plus one job. |
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
| The platform re-runs the job on rollback | none needed | The job is idempotent: roles are created or altered, grants and schema statements repeat safely, and migrations are checksummed and locked. A second run changes nothing. Whether it was a retry or a rollback re-run is UNCONFIRMED (TESTED, inconclusive), which is why idempotence is the rule. | job log lines for `alter_role_*` instead of `create_role_*` on the second run |
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

1. **Retest R1 to R3 results.** Does the migrator CONNECT grant repair the migrator login on a platform-created database, and is `WITH SET TRUE` needed or accepted? If not, which alternative (owner change)? This blocks merging PR #354 and therefore applying this spec.
2. **`${memory-db.PASSWORD}` and `${memory-db.DATABASE}`** are DOCUMENTED but untested bindable names (verification row 4d). If one is not accepted, the job needs the value pasted instead.
3. **Binding shape.** Is `db_user: doadmin` accepted for an attached production cluster in the real `viharbot-stage` spec? The probe used the same form; its result is not recorded as a pass for this exact shape.
4. **Job size.** The job has no `instance_size_slug`, so the platform default applies. Say if a specific size is wanted.
5. **Spec shape for the first apply.** Add the memory pieces to the existing app (this proposal), or create a separate memory app for the window? The design chose the existing app; a separate app would keep Stage untouched and simplify teardown, at the cost of cross-app reachability (a second app could not reach the service in the probe, verification row 2c).
6. **`AGENTS.md`** says "Stage currently has the web service and six agents". It becomes stale once this is deployed; it is a founder-reviewed path and this change does not edit it.
7. **Promotion.** This plan is not a request to deploy. When the build is ready the assistant will post the runbook 4.5 summary and ask the literal question.
