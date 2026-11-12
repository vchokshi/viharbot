# DigitalOcean probe runbook (throwaway)

**Status:** a test kit and a procedure. The founder ran sections 1 to 11 once on 2026-11-12 (results are in `docs/stage-memory-platform-verification.md`, "Tested by throwaway probe"). **Section 8b, the "Retest (2026-11-12 follow-up)", has not been run.** The author of this kit did not provision, deploy or contact DigitalOcean, used no `doctl`, account, token or credential, and did not run any command below. **Every `doctl` command and control-panel step here is something the founder runs by hand.** Every claim about how DigitalOcean behaves is **UNVERIFIED** until the procedure produces the evidence for it, except where a line says "observed 2026-11-12". Implementation under human supervision; nothing here merges or approves anything.

## 1. Purpose, and what this will not do

**Why.** The founder decided that Stage memory runs on App Platform with one Managed PostgreSQL Standard Edition single-node cluster, that the memory service terminates its own TLS with one self-signed certificate (verification on, certificate and key as encrypted environment variables, no secret file mounts documented), that keys and the key registry also arrive as encrypted environment variables, and that 7-day backups are accepted (`docs/stage-memory-platform-verification.md`, `docs/adr/ADR-009-persistent-memory.md`, `docs/memory-service.md`). Several facts that design depends on are **not documented** by DigitalOcean. A short throwaway cluster and app answer them for cents. This kit makes that test fast, safe and unambiguous. (`docs/stage-memory-platform-verification.md` is on the open documentation branch for pull request #322 and may not be on `main` yet.)

**It answers six questions:**

1. Can `doadmin` run SQL `CREATE ROLE ... LOGIN PASSWORD` and `GRANT`, to create a service role and a migrator role like `deploy/postgres/init/10-memory-roles.sh`?
2. Do internal calls arrive unchanged (method, raw target, body bytes), so ADR-006's signed-request binding survives?
3. Which internal hostnames work, and does TLS verification (pinned self-signed certificate, verification on) succeed for them?
4. Can anything outside the app reach an internal-only component?
5. Does a `PRE_DEPLOY` job get the database variables, finish before the service serves, run twice safely, and block the deployment when it fails?
6. What `sslmode` is injected, does `require` work, does `verify-full` work with a pasted CA, and which PostgreSQL version runs?

**It will NOT:**

* provision, deploy or call anything by itself. It is files only. You create and delete everything.
* use or ask for DigitalOcean credentials in this repository, in a pull request or in an issue.
* print or return a secret. The probe components report variable **names**, parsed connection **shapes** (host redacted), and scrubbed error text. They never print passwords, full URLs, keys, tokens or request bodies. This is tested (`tests/test_digitalocean_probe.py`).
* prove Stage is ready, complete a gate, or decide anything. It produces evidence; the founder decides.
* test anything outside the six questions. Not tested: encrypted-variable size limits beyond the roughly 1 KB certificate, key rotation, internal plain-HTTP behaviour, multiple instances, restore from backup, NFS, log retention.
* belong in the product. It is deleted or ignored after the answers are recorded.

**What a pass does and does not mean.** The echo test (question 2) runs with the service terminating its own TLS on an internal port, so the platform has no layer-7 hop to question in that topology. A clean result covers exactly that topology. It does not cover a platform proxy in front of a plain-HTTP internal port, which is not Stage's design.

## 2. Prerequisites

* A DigitalOcean account you control, `doctl` installed and authenticated **by you** (`doctl auth init` is yours to run; never paste the token anywhere), and permission to create apps and databases.
* The kit's branch pushed to GitHub and readable by App Platform's GitHub integration (the specs build from `vchokshi/viharbot`, branch `feature/stage-probe-retest`; change `branch` to `main` after merge, and to any other branch you want to build). Docker is **not** required locally: App Platform builds `deploy/digitalocean-probe/Dockerfile.probe`.
* Python 3.12+ with `cryptography` (already a ViharBot dependency) to run the certificate script, `curl`, and `jq` (optional, for reading results).
* A scratch shell on your laptop. Keep the throwaway values in files under the repository's gitignored `data/` directory (or outside the repository), never in shell history or a ticket.
* Time: about 60 to 90 minutes for one pass. Plan to finish and tear down in one sitting.

## 3. Estimated cost

**All prices below are the list prices already recorded in `docs/stage-memory-platform-verification.md` (retrieved 2026-11-11). They are UNCONFIRMED: confirm current prices on DigitalOcean's pricing pages and in the control panel's cost estimate before you create anything.** I cite no other price.

| Item | Recorded list price | Hourly equivalent (720-hour month assumed) |
|---|---|---|
| App Platform `apps-s-1vcpu-0.5gb` (shared, 512 MiB) | $5.00 per month (verification doc row 8b) | 5.00 / 720 = $0.00694 |
| Standard single-node PostgreSQL, 1 GiB | $15.00 per month (docs pricing page) or $15.15 per month, $0.02254 per hour (marketing page) (row 3a). **Correction from the 2026-11-12 run:** `db-s-1vcpu-1gb` is no longer an offered slug; the run used `db-amd-1vcpu-1gb` (15 GiB storage), whose price was not read from an invoice. These figures belong to the older slug and are an UNCONFIRMED stand-in. | use the higher, cited hourly figure: $0.02254 (UNCONFIRMED for the current slug) |
| Pre-deploy job | billed only while it runs (row 8d), seconds per run | about 0 |

Components: probe-service, probe-client and probe-outsider are three always-on components of $5.00 per month each: 3 x 0.00694 = $0.02083 per hour. Database: $0.02254 per hour. Total: 0.02083 + 0.02254 = **$0.04337 per hour**. For a 4-hour window: 4 x 0.04337 = **$0.17**. For 8 hours: **$0.35**. If you forgot everything for a full day: 24 x 0.04337 = **$1.04**. Databases are billed by the hour with a one-hour minimum and services per second with a one-minute minimum (rows 3c, 8b), so a 20-minute test of the database still costs one hour. Not counted, UNVERIFIED: build minutes, bandwidth, and any charge for the first 28 days wording in row 8b. The control panel's estimate and your invoice are the authority; if they disagree with this table, they win and this table is wrong.

Cost is not a gate here, but a forgotten cluster is the only real risk: do the teardown in section 11.

## 4. Safety rules (read before step 1)

* **Throwaway values only.** The certificate, key, results token and database are created for this test and destroyed after it. Use a new random results token. Never reuse a real ViharBot key, password or certificate.
* **Never paste a secret into an issue, pull request, chat or commit message.** That includes the contents of `data/probe-cert/`, the results token, the database URL and the CA certificate. Record only the PASS/FAIL/INCONCLUSIVE table and the non-secret evidence fields named in section 9.
* **The probe prints no secret values.** It reports variable names, redacted host shapes, SQLSTATEs and scrubbed error text. Still read the logs before you paste any of them anywhere: the app's own environment can contain things this kit does not know about.
* **Public exposure is one read-only page.** probe-client has the only public route; `/health` is open and returns `{"ok": true}`; `/results` needs the bearer token and returns names, shapes and results, never a secret. probe-service has **no** `http_port` and **no** route.
* **Until you add the trusted-source rule in step 6, the cluster accepts connections from anywhere that has valid credentials** (UNVERIFIED default; retest R1 in section 8b measures it). Keep that window to minutes, and the whole test to hours. In the retest the window is deliberate and is flagged in its steps.
* **Destroy within hours.** The teardown is mandatory (section 11).
* The role-bootstrap steps run only in the throwaway database, creating `probe_migrator`, `probe_service` and a `probe_scratch` schema. They never touch ViharBot data (there is none).

## 5. Step by step (all commands are run by the founder)

Everything marked **RUN** is yours. `<angle brackets>` are values you substitute. Set the placeholders in your own shell; do not write them into files that get committed.

### Step 1. Choose the region and VPC, create the cluster

The cluster must be in the datacenter the app's region maps to (or a peered VPC), and only resources in the same VPC can use the private hostname (verification doc rows 4b, 3l).

```text
RUN  doctl apps list-regions                       # UNVERIFIED output columns; confirm which datacenter the app region (spec says nyc) maps to
RUN  doctl vpcs list                               # note the VPC UUID in that datacenter
RUN  doctl databases options slugs --engine pg     # pick a Standard single-node slug from THIS list; confirm version 17 is offered
RUN  doctl databases create probe-pg --engine pg --version 17 --region <datacenter, e.g. nyc1> --size <slug from the list> --num-nodes 1 --private-network-uuid <vpc-uuid>
RUN  doctl databases list --format ID,Name,Status  # note the cluster ID (a UUID); wait until the status is online (about three minutes on 2026-11-12)
```

Observed 2026-11-12: `db-s-1vcpu-1gb` is **not offered any more** (the run used `db-amd-1vcpu-1gb`), and `doctl databases create` has **no `--format` flag** (read the ID from `doctl databases list --format ...` instead).

Control panel alternative: Create, Databases, PostgreSQL 17, Standard (Basic) single node, 1 GiB, the chosen datacenter and VPC. Name it `probe-pg` and put `PLACEHOLDER_CLUSTER_NAME` in `probe-app.yaml` as that name.

### Step 2. Create the dedicated database; note the trusted-source rule comes later

```text
RUN  doctl databases db create <cluster-id> probe
RUN  doctl databases db list <cluster-id>
```

The probe job connects as the cluster's admin user (`doadmin`) to the database named `probe`. The spec's `db_user: doadmin` is UNVERIFIED (verification doc row 4a); if the platform refuses it, see "If a step fails" below.

### Step 3. Download the cluster CA certificate

Documented route: control panel, the cluster, Overview, Connection details, **Download CA certificate** (verification doc row 3k). Save the file to `data/probe-cert/db-ca.crt` (gitignored). Command alternative, UNVERIFIED that it exists in your `doctl`:

```text
RUN  doctl databases get-ca <cluster-id>          # prints or saves the certificate (observed working 2026-11-12)
```

You will paste this file's contents into `PROBE_DB_CA_PEM` in step 5. It is a public certificate, but still keep it out of commits.

### Step 4. Generate the self-signed certificate locally

```text
RUN  python deploy/digitalocean-probe/gen_probe_cert.py --san probe-service --san probe-service.internal --san localhost
```

The default SAN is `probe-service`; add every other name you want verified (repeat `--san`, or comma-separate; IP addresses are allowed). It writes `data/probe-cert/` (gitignored, mode 0600) with `probe.crt.pem`, `probe.key.pem`, `probe.crt.b64`, `probe.key.b64`, valid one year, and prints only the SANs, expiry and certificate fingerprint. The script refuses to write inside the repository outside `data/`. Nothing generated is committed.

Also make a results token and keep it in a file or your password manager:

```text
RUN  python -c "import secrets; print(secrets.token_urlsafe(32))" > data/probe-cert/results-token.txt
```

### Step 5. Create the app from `probe-app.yaml` and paste the secret values

Edit a **copy** if you must change `region`, `branch` or `cluster_name`; the committed file keeps placeholders. The five secret variables have empty values on purpose:

| Variable | Component | Paste |
|---|---|---|
| `PROBE_TLS_CERT_PEM` | probe-service | contents of `probe.crt.b64` |
| `PROBE_TLS_KEY_PEM` | probe-service | contents of `probe.key.b64` |
| `PROBE_TLS_CA_PEM` | probe-client | contents of `probe.crt.b64` (the only certificate it trusts) |
| `PROBE_RESULTS_TOKEN` | probe-client | the token from step 4 |
| `PROBE_DB_CA_PEM` | probe-job | contents of the cluster CA file from step 3 (PEM text, or its base64) |

```text
RUN  doctl apps create --spec deploy/digitalocean-probe/probe-app.yaml     # use your edited copy if you changed placeholders
RUN  doctl apps list                                                        # note the app ID and the live URL
```

Then open the control panel, the app, each component's Settings, Environment Variables, and paste the values as encrypted variables. Pasting multi-line PEM may flatten newlines; that is why the `.b64` files exist (the probe also accepts raw PEM, PEM with literal `\n`, or flattened PEM and reports which form it saw). UNVERIFIED: that an app can be created with empty `SECRET` values and edited afterwards.

Alternative, UNVERIFIED: copy the spec to `data/probe-spec-filled.yaml` (gitignored), put the values into the `value:` fields of that local copy only, and run `doctl apps create --spec data/probe-spec-filled.yaml`. Delete that copy at teardown. The platform encrypts a secret on first submission (verification doc row 7a).

The first deployment starts: build, then the pre-deploy job, then the services. **Do step 6 immediately.**

### Step 6. Add the trusted-source rule, then watch the deployment

```text
RUN  doctl databases firewalls append <cluster-id> --rule app:<app-id>
RUN  doctl databases firewalls list <cluster-id>                            # note the rule UUID for the teardown
RUN  doctl apps list-deployments <app-id>                                   # wait for ACTIVE (or ERROR, then see "If a step fails")
```

Trusted sources block every other connection once enabled (row 4c), including your laptop, which is intended. Observed working 2026-11-12: the `--rule app:<id>` form and `remove --uuid`; the control panel (cluster, Settings, Trusted sources, add the app) is the documented route. That the rule actually **refuses** an untrusted client was not tested in the first run; retest R1 (section 8b) does.

### Step 7. Run the job, read the results

The pre-deploy job ran as part of the first deployment (**run 1**). Read its output:

```text
RUN  doctl apps logs <app-id> probe-job --type deploy      # observed 2026-11-12: job output shows under both `--type deploy` and `--type run`
```

Observed 2026-11-12: `doctl apps logs --deployment <id>` is **refused for superseded deployments** (HTTP 400). Read a deployment's logs while it is the newest one, or use the control panel (Activity, the deployment) afterwards, and copy the lines you need before the next deployment. If the command prints nothing, use the control panel: the app, Activity, the deployment, the pre-deploy job's logs. Find the single line whose JSON has `"event": "probe_job_summary"`. Read the client's results (its logs and page):

```text
RUN  read -rs PROBE_TOKEN < data/probe-cert/results-token.txt     # or paste it; never put the token on a command line you keep
RUN  curl -s -H "Authorization: Bearer $PROBE_TOKEN" https://<live-url>/results | jq '{status, summary, hosts, pinned_certificate}'
RUN  curl -s -H "Authorization: Bearer $PROBE_TOKEN" https://<live-url>/results | jq '.echo.cases[] | select(.exact | not)'
RUN  curl -s -H "Authorization: Bearer $PROBE_TOKEN" https://<live-url>/results | jq '.ordering, .started_at'
```

The client waits up to 3 minutes (`PROBE_STARTUP_WAIT_SECONDS`) for the service, then runs once and logs one `probe_client_summary` JSON line (`doctl apps logs <app-id> probe-client --type run`). `status` is `running` until it finishes; call again. To run it again, redeploy (step 7b).

**7b. Re-run safety (run 2).** Trigger a second deployment; the pre-deploy job runs again:

```text
RUN  doctl apps create-deployment <app-id>
RUN  doctl apps list-deployments <app-id>
```

Read the second `probe_job_summary` and the new client `/results` (the client restarts, so `started_at` is new, and its marker observation now refers to run 2).

**7c. Failing job blocks the deployment (run 3).** In your edited copy of the spec (or the control panel, probe-job, Environment Variables) set `PROBE_JOB_FAIL` to `"true"`:

```text
RUN  doctl apps update <app-id> --spec <your edited spec>
RUN  doctl apps list-deployments <app-id>          # look at the newest deployment's phase and cause
RUN  doctl apps get-deployment <app-id> <deployment-id>
RUN  curl -s -H "Authorization: Bearer $PROBE_TOKEN" https://<live-url>/results | jq '.started_at'   # unchanged from before means the old version kept serving
```

Then set `PROBE_JOB_FAIL` back to `"false"` and deploy again (run 4) so the app is in a known state. Run 4's marker `run_number` should be at least 4: runs 1 to 3 each consumed a number, which proves run 3 executed. **Correction from 2026-11-12:** an extra number appeared between the failed run and the next deployment, and the run counter alone could not say whether the platform retried the failing job or the automated rollback deployment re-ran the previous version's job. Retest R3 (section 8b) separates the two with `PROBE_JOB_TAG`.

### Step 8. Run the outsider app

From the client results, collect the forms that resolved and the private addresses: `jq '.hosts[] | {host, port, resolves, connects, verified, addresses}'`. Put into `probe-outsider-app.yaml` (or a copy): `PROBE_TARGET_HOSTS` (every candidate from the client's list **plus** each private IP and `host:port` that resolved there) and `PROBE_PUBLIC_URLS` (the first app's live URL).

```text
RUN  doctl apps create --spec deploy/digitalocean-probe/probe-outsider-app.yaml
RUN  doctl apps list                                                          # note the outsider app ID
RUN  doctl apps logs <outsider-app-id> probe-outsider --type run              # the line with "probe_outsider_summary"
```

The outsider is in the same region and default VPC, which is the harder isolation case. Optional second run in a different region: change `region` in a copy.

### Step 9. Laptop checks (from outside DigitalOcean)

```text
RUN  curl -sS --max-time 10 -o /dev/null -w '%{http_code}\n' https://<live-url>/                 # client answers (404 JSON): expected, it is the client
RUN  curl -sS --max-time 10 https://<live-url>/__probe/health                                    # expected: {"error": "not found"} from the CLIENT, never {"ok": true}
RUN  curl -sS --max-time 10 https://<live-url>/__probe/info                                      # expected: {"error": "not found"}
RUN  curl -skS --max-time 8 https://<live-url>:8443/__probe/health                               # expected: timeout or connection refused
RUN  curl -skS --max-time 8 https://<private-ip-from-client-results>:8443/__probe/health         # expected: timeout (a private address is not routable from the internet)
RUN  nc -vz -w 5 <live-host> 8443                                                                # expected: fails
RUN  curl -sS --max-time 10 -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer wrong" https://<live-url>/results   # expected: 401
RUN  curl -sS --max-time 10 -X POST -o /dev/null -w '%{http_code}\n' https://<live-url>/results   # expected: 405 (read-only)
```

### Step 10. Record results

Fill in section 9 with PASS, FAIL or INCONCLUSIVE for each row, using only non-secret evidence. Do this **before** teardown (you cannot re-read the evidence afterwards), then tear down.

### If a step fails

* Spec rejected (secret empty, `db_user: doadmin`, `ingress`, health check on an internal service, `internal_ports`): remove the one rejected field in a copy of the spec and note which field and the error text; that is itself a finding (UNVERIFIED in the spec comments). Do not guess around it silently.
* Job cannot connect: confirm the trusted-source rule, that the app and cluster are in the same datacenter or VPC, and that `PROBE_DB_URL` appears in the job summary's `variables.expected_present`. If the variable is missing, record that (fact 5), and as a fallback set `PROBE_DB_URL` yourself as a secret (a `postgresql://` URL for the `probe` database; never paste it elsewhere).
* Client says `PROBE_TLS_CA_PEM` is missing or unusable: re-paste (base64 recommended).
* Verification fails only on the name (`chain_ok_ignoring_hostname` is true): regenerate with that name as a SAN, update `PROBE_TLS_CERT_PEM`, `PROBE_TLS_KEY_PEM` and `PROBE_TLS_CA_PEM`, redeploy, and judge the row INCONCLUSIVE then retest.

## 6. How to read each result (where the evidence lives)

| Source | Where | Contains |
|---|---|---|
| Job summary | pre-deploy job log, line `probe_job_summary` (and `probe_job_attempt_started` before it) | variable names, redacted connection info, `tls[]`, `bootstrap[]`, `bootstrap_summary`, `r2_findings`, `job_tag`, `attempt_id`, `platform_identifiers`, `marker`, `exit_code` |
| Client results | `GET /results` (token) and log line `probe_client_summary` | `hosts[]`, `echo`, `summary`, `ordering`, `marker_now` (live marker counts by tag and mode, ordered attempts), `pinned_certificate`, `environment_variable_names` |
| Outsider summary | outsider worker log, line `probe_outsider_summary` | `targets[]`, `public_urls[]`, `database` (retest R1), `summary` |
| Platform | `doctl apps list-deployments`, `get-deployment`, control panel | deployment phases and causes |
| Laptop | step 9 | HTTP codes, timeouts |

Each `bootstrap[]` step has `step`, `statement` (passwords shown as `'***'`), `expect` (`ok` or `error`), `outcome`, `sqlstate`, scrubbed `error`, and `as_expected`. SQLSTATE 42501 is "insufficient privilege"; classes 08 and 28 are connection and authentication failures (the step did not really test the privilege).

## 7. Judgement rules

The words are used exactly: **PASS** means the evidence shows the property. **FAIL** means the evidence shows the property is absent. **INCONCLUSIVE** means the test did not run, ran on the wrong path, or the evidence does not settle it; fix and retest, never round up to PASS.

## 8. Expected-result tables

### Fact 1: role bootstrap (`bootstrap[]` in the job summary, run 1 unless noted)

| # | Step | Expected on success | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|---|
| 1.1 | `admin_attributes` | `ok`, a `detail` object | record `superuser`, `createrole`, `createdb` (informational) | n/a | step missing or error with class 08 or 28 |
| 1.2 | `create_role_probe_migrator` | `ok` | outcome `ok` | outcome `error` with SQLSTATE 42xxx (record text) | missing, or SQLSTATE class 08 or 28 |
| 1.3 | `create_role_probe_service` | `ok` | outcome `ok` | as 1.2 | as 1.2 |
| 1.4 | `create_schema` | `ok` | `ok` | `error` 42xxx | missing or 08/28 |
| 1.5 | `alter_schema_owner` | `ok` | `ok` | `error`; then read `fallback_*` and `alter_schema_owner_after_membership_grant`: if the fallback is `ok`, the finding is "needs an explicit membership grant" | missing or 08/28 |
| 1.6 | `revoke_public_connect` | `ok` | `ok` | `error` | missing or 08/28 |
| 1.7 | `grant_connect_service` | `ok` | `ok` | `error` | as 1.6 |
| 1.8 | `grant_usage_service` | `ok` | `ok` | `error` | as 1.6 |
| 1.9 | `default_privileges_for_migrator` | `ok` | `ok` | `error` | as 1.6 |
| 1.10 | `set_role_create_table_as_migrator` and `create_table_as_migrator` | both `ok` | both `ok` | either `error` (see the fallback rows) | missing |
| 1.11 | `service_reads_migrator_table_via_default_privileges` | `ok` | `ok` (default privileges reach the service role) | `error` | missing because 1.10 failed |
| 1.12 | `service_cannot_create_table_expected_error` | `error` (permission denied) | outcome `error`, `as_expected` true | outcome `ok` (the service role holds DDL rights) | missing |
| 1.13 | `login_as_probe_migrator`, `login_as_probe_service` | `ok` | both `ok` (the new roles can log in) | `error` 28xxx | `error` class 08 (blocked by network or trusted sources) |

Note (2026-11-12): row 1.13's `login_as_probe_migrator` failed with SQLSTATE 42501 for lack of CONNECT on the platform-created database. That step, its position and its expected result are unchanged so the failure stays visible; retest R2 adds the repair steps after it.

**Fact 1 overall:** PASS only if 1.2 to 1.13 are all PASS on run 1 (the `fallback_*` rows may exist but only inform). FAIL if any role, schema, grant or privilege step is FAIL. INCONCLUSIVE if the job never connected or the summary is missing. Record, per failing step, the SQLSTATE and the error text from the summary (already scrubbed).

### Fact 2: internal calls arrive unchanged (`echo` in `/results`)

Cases: `get-plain`, `get-percent-space`, `get-query-order-and-repeat`, `get-query-order-reversed`, `get-trailing-empty-query`, `get-bare-and-empty-values`, `get-hex-case-upper`, `get-hex-case-lower`, `get-plus-vs-percent`, `get-path-not-normalized`, `get-utf8-percent`, `post-empty`, `post-small-json`, `post-large-300kb`, `post-non-ascii`, `put-body`, `delete-empty`, `patch-body` (18).

| Check | Expected | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|
| Every case `exact` | all 18 true: method, `raw_target`, `body_sha256`, `body_length` equal what was sent | `echo.all_exact` true and `summary.echo_cases` is 18 | any case `exact` false: record the case, the `diffs` field names and the sent/received targets (targets are not secret) | `echo.cases` empty (no host connected), or a case has an `error` (timeout, status): re-run; if only the large case errors, lower `PROBE_LARGE_BODY_KB` and record the size limit as a separate finding |
| Query order and duplicates kept | `get-query-order-*` exact | both exact | a reorder or merge | as above |
| Hex case kept | `get-hex-case-upper` and `-lower` exact | both exact | a case change | as above |
| Trailing `?` kept | `get-trailing-empty-query` exact | exact | `?` dropped | as above |
| Body bytes kept | `post-empty`, `post-small-json`, `post-large-300kb`, `post-non-ascii` exact | all exact | any hash or length differs | as above |
| Transport | `echo.transport.kind` is `verified-tls` | `verified-tls` | n/a | `unverified-fallback`: routing is still judged on the exact fields (a PASS for fact 2) but fact 3 is not a PASS |
| `notes` (informational) | none, or headers added | record added header names | never a FAIL by itself (ADR-006 binds method, target, body) | n/a |

**Decision.** Fact 2 PASS means the ADR-006 binding (`mth`, `tgt`, `req_sha256`) survived this path. FAIL means it did not and ADR-006's "a proxy that rewrites the target must be configured to preserve it" cannot be met on this path; record exactly which transformation happened.

### Fact 3: internal hostname and TLS (`hosts[]` in `/results`)

| Check | Expected | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|
| Plain name `probe-service` | resolves, connects, verifies | `resolves`, `connects`, `verified` all true | `resolves` false, or resolves but `connects` false | `connects` true, `verified` false and `chain_ok_ignoring_hostname` true (the name is not in the certificate: regenerate with it and retest) |
| Any other form (each candidate row) | record, per row, `resolves`, `connects`, `verified`, `reason` | informational | informational | informational |
| Pinned certificate only | verification on, no system roots | the unit tests prove this; evidence is `verified` true only for the pinned certificate | `connects` true, `verified` false, `chain_ok_ignoring_hostname` false for every row: the served certificate is not the pinned one (check the pasted variables match, or something terminates TLS in front of the service) | service log says `refusing to start without a certificate`, or `pinned_certificate` is missing: fix the variables |
| Variable form survived | `variable_forms` in the `probe_service_started` log and `pinned_certificate.variable_form` | the service started and the form is reported | the service refuses to start for a certificate or key that was pasted correctly (record `variable_forms`, and the character counts: it suggests the platform altered or truncated the value) | n/a |

**Fact 3 overall:** PASS if at least one candidate is `verified` (record which hostname forms resolve, which verify, and the private addresses). FAIL if no candidate resolves, or none verifies for a reason other than the name. INCONCLUSIVE if the only failure is a name missing from the certificate, or the service never started.

### Fact 4: reachability (`probe_outsider_summary`, plus step 9)

| Check | Expected | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|
| Outsider to internal names and IPs | nothing answers | `targets_answering_as_probe_service` is empty, and the outsider's list included every name and private IP that the client resolved | any target answers as probe-service: another app can reach an internal-only service (record the names; app-layer authentication is then the only isolation) | the list was missing a name or IP the client resolved, the first app was not running during the run, or no `probe_outsider_summary` line exists |
| Outsider to public URL | the service has no public route | `public_urls_answering_as_probe_service` is empty | any public URL returns `{"ok": true}` for `/__probe/health` | URL not configured or the DNS name did not resolve |
| Laptop (step 9) | `/__probe/health` and `/__probe/info` return the client's 404; port 8443 and private IPs do not answer | all as listed | any `{"ok": true}` or an answer on 8443 from the internet | a network problem on your side (test from a second network) |

**Fact 4 overall:** PASS means the internal service had no route from outside the app and none from a second app in the same region (UNVERIFIED for other VPCs or accounts, not tested). FAIL if anything outside reaches it. Remember a negative result only counts for names the first app could itself resolve.

### Fact 5: pre-deploy job (job summary, `ordering` in `/results`, deployment phases)

| Check | Expected | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|
| Database variables reach the job | `variables.expected_present.PROBE_DB_URL` true and `connection_info` shows host redacted, port, database, `sslmode_in_url` | present and parsed | `PROBE_DB_URL` false (or empty) in the job (record `variables.database_like_names`) | the job log is missing |
| Job exits 0 and writes the marker | `exit_code` 0 and `marker.written` true | both | `marker.written` false with the job connected (record `marker.errors`) | no connection (see 1) |
| Ordering | the marker existed at the service's first request | in `/results`, `ordering.first_health_check.marker.found` is true and its `latest_run_number` equals this deployment's job `marker.run_number`, and the client's `started_at` is later than the job's `finished_at` | `found` is false at the first health check while the job's `marker.written` is true (the service served before the job finished), or `latest_run_number` is older than the job's | `found` is null (the client cannot reach the database), or this is not a fresh deployment and the run numbers cannot be matched |
| Re-run safety (run 2) | second job exits 0 with no unexpected step | `bootstrap_summary.rerun_detected` true, `unexpected` empty, `exit_code` 0, `marker.run_number` 2, and its CREATE ROLE steps appear as `alter_role_..._rerun` | the second run errors or exits non-zero | only one run was made |
| Failing job blocks deploy (run 3) | deployment fails, old version keeps serving | the newest deployment shows an error phase, the job log shows `exit_code` 3 and `job_mode` `fail`, `/results` `started_at` is unchanged, and run 4's `marker.run_number` is at least 4 (each retry of the failing job consumes a number) | the deployment goes ACTIVE, or the client restarted on the new version after the failed job | the deployment failed for another reason (for example a build error and no job line), or you did not check `started_at` |
| Retry behaviour (information) | number of `probe_job_summary` lines in the failed deployment's job logs | record the count (more than 1 means the platform retried) | n/a | n/a. On 2026-11-12 this was INCONCLUSIVE (one job run seen, one extra marker number). Use retest R3 (section 8b), which tags every attempt. |

### Fact 6: database TLS (`tls[]` and `connection_info` in the job summary)

| Check | Expected | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|
| `sslmode` in injected info | record `connection_info.sslmode_in_url` (`(absent)` if there is no parameter) | informational (the docs say the default shown is `require`; this records what the app actually gets) | n/a | `connection_info` has an `error` |
| `host_has_private_prefix` | record | informational | n/a | n/a |
| `sslmode_require` | `ok`, `tls_in_use` true, `tls_version` recorded | `ok` and `tls_in_use` true | `error` (record SQLSTATE and text) | no connection information |
| `verify_full_with_supplied_ca` | `ok` | `ok` | `error` (record the text: a hostname mismatch with the private hostname, or a wrong CA) | `not_run` (`PROBE_DB_CA_PEM` not set or unusable): paste the CA and rerun |
| `verify_ca_with_supplied_ca` (information) | record | informational | informational | informational |
| `verify_full_system_roots` (information, Advanced Edition route) | record | informational | informational | informational |
| PostgreSQL version | `server_version` begins with `17` | begins with 17 | another major version (record it) | missing |

**Fact 6 overall:** the answer to "can the memory service verify the database with `verify-full` and a pasted CA" is PASS only if `verify_full_with_supplied_ca` is `ok`. FAIL means verification works only as `require`, which does not authenticate the server (ADR-009 requires authenticated TLS); record whether `verify-ca` works.

## 8b. Retest (2026-11-12 follow-up)

### 8b.1 Purpose

**Why.** The first run (2026-11-12) left three points open that change what the Stage bootstrap may claim and what SQL it runs (`docs/stage-memory-design.md`, sections 4 and 7.3; decision N3, accepted by the founder, says to run this retest once on a throwaway cluster before the bootstrap entry point is built; decision N4 allows a temporary operator trusted-source rule for the later conformance run, not needed here).

| Retest | Open point | Kit change |
|---|---|---|
| **R1** | The first run proved the permitted app connects. It did not prove that a trusted-source rule **refuses** anyone else. | `probe_outsider.py` database check, `probe_laptop_dbcheck.py`, steps ordered so the denial is shown as a change caused by the rule |
| **R2** | The migrator could not log in to a platform-created database (no CONNECT). Does the repair work, can the owner be changed, and does `CREATE SCHEMA IF NOT EXISTS` fail for a migrator that owns the schema but lacks CREATE on the database (the `migrate.py` problem)? | new `probe_job.py` steps after `login_as_probe_service`, with the original steps unchanged |
| **R3** | A failing pre-deploy job produced an extra marker number, and it could not be told whether the platform **retried** the job or the automated **rollback** deployment re-ran the previous version's job. | `PROBE_JOB_TAG`, a per-attempt id and start time in every marker row, and `marker_now` in the client's `/results` |

This retest produces evidence. It does not prove Stage is ready, complete a gate, decide anything, or touch ViharBot data (there is none).

### 8b.2 Extra cost and safety rules

**Cost.** One more short-lived cluster and the same three always-on components (probe-service, probe-client, probe-outsider), so the same hourly rate as section 3: about **$0.04337 per hour** (UNCONFIRMED for the current slug `db-amd-1vcpu-1gb`; see section 3). The retest needs about 2 hours: 2 x 0.04337 = **$0.09**, and **$0.17** if it stretches to 4. The scratch databases live for seconds inside the same cluster and cost nothing extra. The outsider's one-minute loop adds a few kilobytes. If you forget everything for a day: **$1.04**. The control panel estimate and your invoice are the authority.

**Safety rules (all of section 4 still applies, plus):**

* **This retest opens an exposure window on purpose** so that denial can be shown as a change. The window runs from the moment the cluster is online until `firewalls append` returns in step R1-5. Target: **under 20 minutes**. During it the cluster accepts connections from anywhere that has valid credentials (UNVERIFIED; this is what R1 measures). The cluster is empty and the only credentials that work are throwaway ones. **Do not put any real data or a real credential in it.**
* The outsider and the laptop check use a **throwaway database user** (`probe_denial`), never `doadmin`. Keep its password in `data/probe-cert/db-denial.env` (gitignored) or your password manager. Never paste it, the database host or the database URL into an issue, pull request, chat or commit. Both checks print only a classification, redacted host shape, SQLSTATE and scrubbed text.
* `ALTER DATABASE ... OWNER TO` runs **only in two scratch databases** (`probe_scratch_db_a`, `probe_scratch_db_b`) that the job creates and drops in the same run. The `probe` database's owner is never changed.
* Do not start another deployment while R3 is waiting for its answer (step R3-3); an extra deployment would add rows and spoil the reading.

### 8b.3 What each check records, and where

| Retest | Evidence | Where |
|---|---|---|
| R1 outsider | `database.classification`, `stopped_at`, `stages`, plus `summary.database_classification` and `summary.database_reached` | outsider log line `probe_outsider_summary` (one per interval) |
| R1 laptop | the same object, printed as JSON; exit status 0 = not reached, 1 = reached, 2 = could not run | your terminal |
| R2 | `bootstrap[]` steps (the names are listed in 8b.5), `r2_findings`, `bootstrap_summary` | job log line `probe_job_summary` |
| R3 | `marker_now` (counts by tag and mode, ordered attempts with tag, mode, `started_at`, `attempt_id`, `run_number`, `platform_identifiers`) | client `GET /results`; also job log lines `probe_job_attempt_started` while a deployment is still the newest one |

Outsider `classification` values, in the order a connection progresses: `dns_failure`, `timeout` (TCP got no answer), `connection_refused`, `tcp_error`, `tls_failed` (TCP connected, then the server closed, did not answer or failed the TLS handshake), `tls_not_offered`, `tls_ok_authentication_refused` (TLS worked, then the login was refused: SQLSTATE class 28), `login_failed_other`, `login_not_run` (psycopg missing), `login_ok_connected`, and `not_configured`. `stopped_at` names the stage (`dns`, `tcp`, `tls`, `login`) or is null on success. The TLS stage does not verify the server certificate (it only asks whether the address is let in); the login stage uses `sslmode=require`, or `verify-full` when `PROBE_OUTSIDER_DB_CA_PEM` is set.

### 8b.4 Steps, in order (all commands are run by the founder; I did not run any of them)

`<angle brackets>` are values you substitute. Everything marked **RUN** is yours. **UNVERIFIED** flags a `doctl` flag or behaviour that has not been observed; each has a control-panel fallback. Write the UTC time (`date -u`) next to each step in your notes.

**R1-0. Prepare.** Push or check out the kit on the branch the specs build from (`feature/stage-probe-retest`, or `main` after merge). Make a scratch file `data/probe-cert/db-denial.env` (gitignored) for the throwaway user's details. Prerequisites in section 2 apply.

**R1-1. Cluster, dedicated database, CA, throwaway user. (The exposure window opens here.)**

```text
RUN  doctl databases options slugs --engine pg                 # choose a Standard single-node slug from THIS list (db-s-1vcpu-1gb is not offered any more)
RUN  doctl databases create probe-pg --engine pg --version 17 --region <datacenter, e.g. nyc1> --size <slug> --num-nodes 1 --private-network-uuid <vpc-uuid>     # no --format flag on create
RUN  doctl databases list --format ID,Name,Status               # note the cluster ID; wait for online
RUN  doctl databases db create <cluster-id> probe
RUN  doctl databases get-ca <cluster-id>                        # save to data/probe-cert/db-ca.crt
RUN  doctl databases user create <cluster-id> probe_denial      # UNVERIFIED: prints the user's password once; copy it only into data/probe-cert/db-denial.env
RUN  doctl databases connection <cluster-id> --format Host,Port     # public host and port; add --private for the private host. UNVERIFIED flags
```

Control panel fallback: Databases, `probe-pg`, Users & Databases, Add user `probe_denial` (copy its password once); Overview, Connection details (public network and VPC network tabs) for hosts and the port. Record the cluster's VPC and whether it is the region's default VPC.

**R1-2. Main app, first deployment. (Window still open; this deployment also gives the first R2 and R3 evidence.)** Follow section 5 steps 4 and 5 (certificate, results token, `doctl apps create --spec deploy/digitalocean-probe/probe-app.yaml`, paste the five secret variables). The job's `PROBE_JOB_TAG` is `ok-1` as committed. **Do not add the trusted-source rule yet.** Wait for ACTIVE (`doctl apps list-deployments <app-id>`). While it is the newest deployment, read the job log (section 5 step 7) and save the `probe_job_summary` line; this is R2's evidence. Note the app ID.

**R1-3. Outsider app, BEFORE the rule (expected: the database accepts the outsider).** In a **copy** of `probe-outsider-app.yaml` (not committed) set `PROBE_OUTSIDER_INTERVAL` to `"60"`, `PROBE_TARGET_HOSTS` and `PROBE_PUBLIC_URLS` to empty, and leave the four DB secrets empty:

```text
RUN  doctl apps create --spec <your copy of probe-outsider-app.yaml>
RUN  doctl apps list                                           # note the outsider app ID
```

In the control panel set the encrypted variables on `probe-outsider`: `PROBE_OUTSIDER_DB_HOST` (the **private** host if the cluster is in the app region's default VPC, the harder case; else the public host), `PROBE_OUTSIDER_DB_USER` (`probe_denial`), `PROBE_OUTSIDER_DB_PASSWORD`, and optionally `PROBE_OUTSIDER_DB_CA_PEM` (the CA file). Saving redeploys the worker. Then read its log:

```text
RUN  doctl apps logs <outsider-app-id> probe-outsider --type run      # lines with "probe_outsider_summary"; read .database and .summary
```

Wait for **two** consecutive lines. Expected with no rule: `login_ok_connected`. If you see anything else, stop the retest here: the check has not shown the default state (wrong host, user, password or network) and R1 will be INCONCLUSIVE until fixed.

**R1-4. Laptop, BEFORE the rule (expected: connects).** From your own network (not the cloud, not a VPN into DigitalOcean). Type the password without echo; nothing goes on a command line:

```text
RUN  read -rs PROBE_LAPTOP_DB_PASSWORD; export PROBE_LAPTOP_DB_PASSWORD
RUN  export PROBE_LAPTOP_DB_HOST=<public host> PROBE_LAPTOP_DB_USER=probe_denial PROBE_LAPTOP_DB_PORT=<port>
RUN  python deploy/digitalocean-probe/probe_laptop_dbcheck.py ; echo "exit=$?"      # needs psycopg for the login stage: pip install "psycopg[binary]"
```

Expected before the rule: `"classification": "login_ok_connected"`, exit 1. Without Python, the same attempt by hand (the password is prompted for; `psql` never shows it):

```text
RUN  nc -vz -w 5 <public host> <port>                          # TCP only
RUN  psql "host=<public host> port=<port> dbname=defaultdb user=probe_denial sslmode=require connect_timeout=10" -c "select 1"
```

**R1-5. Add the trusted-source rule. (The exposure window closes when this returns.)**

```text
RUN  date -u                                                   # note the time
RUN  doctl databases firewalls append <cluster-id> --rule app:<app-id>      # the MAIN app only; observed working 2026-11-12
RUN  doctl databases firewalls list <cluster-id>               # note the rule UUID (for teardown); it must list the main app and nothing else
```

Control panel: cluster, Settings, Trusted sources, add the main app only. **Do not add the outsider app, your laptop's address or any other source.**

**R1-6. AFTER the rule (expected: refused).** Wait **two minutes** (UNVERIFIED how fast a rule takes effect), then:

```text
RUN  doctl apps logs <outsider-app-id> probe-outsider --type run      # at least two lines timestamped after the wait
RUN  python deploy/digitalocean-probe/probe_laptop_dbcheck.py ; echo "exit=$?"      # twice, a minute apart
RUN  nc -vz -w 5 <public host> <port> ; psql "host=<public host> port=<port> dbname=defaultdb user=probe_denial sslmode=require connect_timeout=10" -c "select 1"
```

Expected: not `login_ok_connected`; laptop exit 0. Record the classification and `stopped_at` of every observation (for example `timeout` at `tcp` means the address is dropped at the network edge; `tls_ok_authentication_refused` means the server answered and refused at login). UNVERIFIED which of these DigitalOcean produces.

**R1-7. The permitted app still connects.** Trigger a second deployment of the main app (`doctl apps create-deployment <app-id>`); the pre-deploy job connects from the trusted app. Read its `probe_job_summary` (while it is the newest deployment): `tls[0].outcome` must be `ok` and `bootstrap` must be present. This is also R2's re-run (run 2) and R3's second `ok-1` row.

**R2 evidence.** Read `bootstrap[]` in the run-1 and run-2 job summaries (from R1-2 and R1-7) for the steps in 8b.5 and `r2_findings`. Nothing more is run for R2; the steps are part of every job run. They are idempotent, so run 2 also tests re-run safety.

**R3-1. Baseline.** After R1-7, read the live marker and save it. Do this immediately before the failing deployment:

```text
RUN  read -rs PROBE_TOKEN < data/probe-cert/results-token.txt
RUN  curl -s -H "Authorization: Bearer $PROBE_TOKEN" https://<live-url>/results | jq '.marker_now | {rows, tag_columns, by_tag_mode, attempts: [.attempts[] | {run_number, tag, mode, started_at, attempt_id, platform_identifiers}]}'
```

Expected: `tag_columns` true, two `ok-1` rows (runs 1 and 2). Note `rows` as **N0**. If `tag_columns` is false, the job could not add the columns (record `tag_columns_error` and the job's `marker.errors_before_success`) and R3 is INCONCLUSIVE until fixed.

**R3-2. The failing deployment.** In your edited copy of `probe-app.yaml` (or the control panel, probe-job, Environment Variables) set **`PROBE_JOB_FAIL` to `"true"` and `PROBE_JOB_TAG` to `"fail-1"`**. The normal spec keeps `ok-1`. This difference is the whole instrument:

```text
RUN  doctl apps update <app-id> --spec <your edited spec>
RUN  doctl apps list-deployments <app-id>                      # watch: the failed deployment, then a rollback deployment; note both IDs and causes
RUN  doctl apps get-deployment <app-id> <deployment-id>
```

**R3-3. Wait, then read twice.** Do not start any other deployment. After the rollback deployment finishes (seconds on 2026-11-12) wait 5 minutes, read `marker_now` as in R3-1, wait 2 minutes, read it again. If the failed deployment's job log is still readable (`doctl apps logs <app-id> probe-job --type deploy`, newest deployment only; `--deployment <id>` is refused for superseded ones, HTTP 400), count its `probe_job_attempt_started` lines and keep them as a cross-check.

**R3-4. Read the counts.** From the second read, using only rows **after** the baseline (`run_number` greater than N0): **f** = rows tagged `fail-1` (mode `fail`), **o** = rows tagged `ok-1` whose position in `attempts` is after the first `fail-1` row.

**R3-5. Restore.** Set `PROBE_JOB_FAIL` back to `"false"` and `PROBE_JOB_TAG` to `"ok-1"` and deploy (`doctl apps update <app-id> --spec <spec>`), so the app is in a known state before teardown.

### 8b.5 Expected-result tables and rules

The words are used as in section 7. **PASS** means the evidence shows the property, **FAIL** that it shows the property is absent, **INCONCLUSIVE** that the test did not run, ran on the wrong path or does not settle it; fix and retest, never round up.

#### R1: trusted-source denial

| # | Observation | Expected | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|---|
| 1.a | Outsider, BEFORE the rule (two consecutive lines) | `login_ok_connected` | both lines `login_ok_connected`: the default state is open and the credential works | n/a (this row documents the default; a different result stops the retest, see R1-3) | any other classification, or one line only |
| 1.b | Laptop, BEFORE the rule | `login_ok_connected`, exit 1 | as expected | n/a | any other result (network on your side, `login_not_run`, wrong details) |
| 1.c | Outsider, AFTER the rule, at least two consecutive lines timestamped at least two minutes after R1-5 | not `login_ok_connected`: `timeout`, `connection_refused`, `tcp_error`, `tls_failed`, `tls_not_offered` or `tls_ok_authentication_refused` | rows 1.a and 1.b both PASS and every AFTER line is one of the refusal classes | any AFTER line is `login_ok_connected` (an app in the same region and VPC that is not a trusted source still connects: the rule cannot be relied on for that path) | 1.a was not PASS (a refusal could be a typo), the lines are `login_failed_other`, `login_not_run`, `dns_failure` or `not_configured`, only one AFTER line exists, or `firewalls list` did not show the rule |
| 1.d | Laptop, AFTER the rule, twice a minute apart | exit 0 and a refusal class | as 1.c for the laptop | `login_ok_connected` or exit 1 (an untrusted public address still connects) | as 1.c |
| 1.e | Main app's job after the rule (R1-7) | `tls[0].outcome` `ok`, `bootstrap` present | as expected | the job cannot connect (the rule did not match the app: record `connect_as_admin` or the `tls[]` errors) | no second deployment was made |
| 1.f | Where it stops (information) | record `stopped_at` and `classification` for each AFTER observation, both paths | informational | informational | informational |

**R1 overall:** PASS only if 1.a to 1.e are PASS: the denial is a **change caused by the rule** (open before, refused after, permitted app still works). FAIL if 1.c or 1.d is FAIL: record that, and the design says the rule cannot be relied on instead of assuming it. INCONCLUSIVE otherwise. This tests two untrusted sources (a sibling app in the same account, region and probably VPC, and a laptop on the public internet); other accounts and VPCs are not tested.

#### R2: migrator privileges on a platform-created database

Read `bootstrap[]` in the **run-1** job summary (a fresh cluster), then confirm run 2. `outcome` is `ok`, `error` or `skipped`; a `skipped` step means something it depends on failed.

The new steps, in the order the job runs them (after `login_as_probe_service`; each record has `step`, `statement` with passwords as `'***'`, `expect`, `outcome`, `sqlstate`, scrubbed `error`, `as_expected`): `grant_connect_migrator`, `login_as_probe_migrator_after_connect_grant`, `scratch_database_cleanup_before_a`, `scratch_database_cleanup_before_b`, `reset_set_option_on_migrator`, `membership_state_before_variant_a`, `create_scratch_database_a`, `alter_scratch_database_owner_without_membership_grant`, `grant_migrator_to_admin_with_set_true` (with `grant_migrator_to_admin_plain` only if that is refused), `create_scratch_database_b`, `alter_scratch_database_owner_after_membership_grant`, `scratch_database_owners`, `drop_scratch_database_a`, `drop_scratch_database_b`, `precondition_migrator_connect_yes_create_no`, `precondition_schema_owned_by_migrator`, `migrator_create_schema_if_not_exists_existing_schema`, `migrator_existence_check_then_skip`, `migrator_creates_table_in_existing_schema`, `migrator_create_other_schema_expect_error`, `drop_other_schema_cleanup`. Steps whose `expect` is `any` (the cleanup, reset, membership-state and owners steps) are informational and always count as expected.

| # | Steps | Expected | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|---|
| 2.0 | `login_as_probe_migrator` (the original step, unchanged) | on a fresh cluster `error`, SQLSTATE 42501, as on 2026-11-12 | record it (reproduces the failure) | n/a (information; `ok` on run 1 means the platform already allows it: note it) | n/a |
| 2.1 | `grant_connect_migrator` | `ok` | `ok` | `error` (record SQLSTATE and text) | missing, or SQLSTATE class 08 or 28 |
| 2.2 | `login_as_probe_migrator_after_connect_grant` | `ok` | `ok`: the CONNECT grant repairs the login | `error`: record SQLSTATE; the design then falls back to the owner change (2.3 and 2.4) | `skipped`, or missing |
| 2.3 | `create_scratch_database_a`, then `alter_scratch_database_owner_without_membership_grant` (variant A: no membership grant first) | informational: either result is a finding | both `ok`: the owner can be changed without `GRANT migrator TO CURRENT_USER` | `error` (record SQLSTATE and text; then read 2.4) | `create_scratch_database_a` is `error`, or the alter is `skipped`: nothing was tested |
| 2.4 | `grant_migrator_to_admin_with_set_true` (or `grant_migrator_to_admin_plain` if the first is refused), `create_scratch_database_b`, then `alter_scratch_database_owner_after_membership_grant` (variant B) | `ok` | all `ok`: with the membership grant the owner can be changed | the grant or the alter is `error` | a `skipped` step |
| 2.5 | `precondition_migrator_connect_yes_create_no`, `precondition_schema_owned_by_migrator` (rows `["True", "False"]` and `["probe_migrator"]`) | both `as_expected` true | both true: rows 2.6 to 2.8 are meaningful | n/a | either false: 2.6 to 2.8 are INCONCLUSIVE (the migrator has CREATE on the database, or does not own the schema) |
| 2.6 | `migrator_create_schema_if_not_exists_existing_schema` | `error`, SQLSTATE 42501, text "permission denied for database" | PASS (the `migrate.py` problem is real on DigitalOcean; the existence-check change is needed) | `ok`: it succeeds on DigitalOcean, so the change stays only as defence in depth | `skipped`, or 2.5 not PASS |
| 2.7 | `migrator_existence_check_then_skip`, `migrator_creates_table_in_existing_schema` | both `ok` | both `ok`: look the schema up, skip the create, and the migrator can still work | either `error` | `skipped` |
| 2.8 | `migrator_create_other_schema_expect_error` (then `drop_other_schema_cleanup`) | `error`, SQLSTATE 42501 | PASS: the migrator cannot create any other schema | `ok`: the migrator can create schemas (record why; the cleanup step removed it) | `skipped`, or 2.5 not PASS |
| 2.9 | Idempotency: run 2's job (R1-7) | exit 0; the same outcomes for 2.1 to 2.8 except 2.0 and 2.2 (the grant persists, so the original migrator login is now `ok`); `drop_scratch_database_a` and `_b` `ok` | as expected, and no scratch database is left | the second run errors, exits non-zero, or leaves a scratch database | only one run |

**Reading 2.3 and 2.4 together.** A `ok` and B `ok`: the owner change works without a membership grant, so the grant is redundant. A `error` and B `ok`: the bootstrap **needs** `GRANT <migrator> TO CURRENT_USER WITH SET TRUE` before any owner change. Both `error`: the owner change cannot be done as `doadmin` on this platform; use the CONNECT grant (2.1, 2.2) only. Neither variant touches the `probe` database's owner.

**R2 overall:** PASS if 2.1 and 2.2 are PASS (the repair works), 2.5 is PASS, and 2.6 to 2.9 each have a recorded outcome that is PASS or the stated FAIL. Report each row separately; do not average them. INCONCLUSIVE if the job never connected or the summary is missing.

#### R3: retry versus rollback

Counts come from R3-4 (rows after the baseline). `attempt_id` must differ between rows (the same id twice would be a duplicate read, not two attempts).

| f (`fail-1` rows) | o (`ok-1` rows after the first `fail-1` row) | Reading |
|---|---|---|
| 0 | any | INCONCLUSIVE: the failing job wrote no marker (it did not run, could not connect, or failed before the write). Record the failed deployment's cause. |
| 1 | 0 | PASS: one attempt, **no retry**, and the rollback did **not** re-run the job (or it ran without writing a row). |
| 1 | 1 or more | PASS: **no retry**; the rollback deployment re-ran the previous version's job o time(s). |
| 2 or more | 0 | PASS: the platform **retried** the failing job (f attempts, f minus 1 retries); no rollback re-run. |
| 2 or more | 1 or more | PASS for both: f attempts of the failing job, and the rollback re-ran the old job o time(s). |

| Condition | Rule |
|---|---|
| `tag_columns` false, `marker_now.found` null or an `error` | INCONCLUSIVE (the live read did not work) |
| The two reads (R3-3) differ | INCONCLUSIVE: the counts were still changing; wait and read again before deciding |
| Extra `fail-1` rows (f is 2 or more) while `list-deployments` also shows a rollback deployment | The tag alone cannot prove a rollback did not re-run the *failing* spec. Compare `platform_identifiers` of the extra rows with the deployment IDs from R3-2 (UNVERIFIED that the platform exposes any; the job lists every identifier-like variable **name** it finds under `names_found` and records values only for plain, non-secret ones). Without a distinguishing identifier, call f an upper bound on retries and the row INCONCLUSIVE for "retried". |
| `ok-1` appears after `fail-1` | It is evidence that a job with the **previous** spec ran after the failure, which is how the rollback is expected to behave (UNVERIFIED that a rollback runs the previous deployment's spec; the tag shows it if so). |
| Answer for the design | Record "runs per failed deployment" as the f and o of the table, and whether every R2 step was safe at that count (it is: R2's steps are idempotent, and run 2 passed). If still INCONCLUSIVE, keep "migrations must be idempotent" as the rule. |

**R3 overall:** PASS if the table row is PASS and the two reads agree. It answers one question: how many job runs a failed deployment produced, and who made them.

### 8b.6 Results template (copy into your notes; do not paste secrets)

Date, who ran it, kit commit hash, region, cluster slug and PostgreSQL version, VPC (default or not), private or public host used by the outsider: ______

| # | Retest | Result | Evidence (non-secret fields only) |
|---|---|---|---|
| R1.a | Outsider BEFORE: connects | PASS / FAIL / INCONCLUSIVE | `classification` of each line, times |
| R1.b | Laptop BEFORE: connects | PASS / FAIL / INCONCLUSIVE | `classification`, exit code |
| R1.c | Outsider AFTER: refused | PASS / FAIL / INCONCLUSIVE | `classification` and `stopped_at` of each line, minutes since the rule |
| R1.d | Laptop AFTER: refused | PASS / FAIL / INCONCLUSIVE | `classification`, `stopped_at`, exit code, `nc` and `psql` messages |
| R1.e | Permitted app still connects | PASS / FAIL / INCONCLUSIVE | job `tls[0].outcome` after the rule |
| R1.f | Exposure window length | minutes | cluster-online time to rule-added time |
| R2.1-2.2 | CONNECT grant, then migrator login | PASS / FAIL / INCONCLUSIVE | steps `grant_connect_migrator`, `login_as_probe_migrator_after_connect_grant`, and the original `login_as_probe_migrator` |
| R2.3-2.4 | Owner change in scratch databases (A without, B with a membership grant) | recorded: A ok / error, B ok / error | SQLSTATE and text per step |
| R2.6 | `CREATE SCHEMA IF NOT EXISTS` on the existing schema as the migrator | PASS / FAIL / INCONCLUSIVE | `sqlstate`, `error` text, precondition rows |
| R2.7 | Existence check then skip works | PASS / FAIL / INCONCLUSIVE | the two steps |
| R2.8 | Migrator cannot create another schema | PASS / FAIL / INCONCLUSIVE | `sqlstate` |
| R2.9 | Re-run safety of the new steps | PASS / FAIL / INCONCLUSIVE | run 2 `bootstrap_summary` |
| R3 | Retry versus rollback | PASS / FAIL / INCONCLUSIVE | baseline N0, f, o, `by_tag_mode`, ordered `(tag, mode, started_at)`, deployment IDs and causes, `names_found` |

Also record, as findings that are not rows: `doctl` flags that did not exist, how quickly the rule took effect, which refusal DigitalOcean produces, any surprise, and the cost on the bill after teardown. Do the teardown (section 11, with the retest additions) the same day.

## 9. Results template (copy into your notes; do not paste secrets)

Date, who ran it, kit commit hash, region, cluster size and version, edition, app regions: ______

| # | Fact | Result | Evidence (non-secret fields only) |
|---|---|---|---|
| 1 | Role bootstrap (`CREATE ROLE`, `GRANT` as doadmin) | PASS / FAIL / INCONCLUSIVE | failing steps with SQLSTATE and text; `admin_attributes` detail |
| 2 | Internal calls arrive unchanged | PASS / FAIL / INCONCLUSIVE | `echo.summary`, any non-exact case and its diffs, `transport.kind` |
| 3 | Internal hostname and TLS (pinned certificate) | PASS / FAIL / INCONCLUSIVE | working hostname forms, resolved private addresses, `verified` per row, `variable_forms` |
| 4 | Reachability (outsider app and laptop) | PASS / FAIL / INCONCLUSIVE | outsider `summary`, curl codes, names tried |
| 5a | Job receives database variables | PASS / FAIL / INCONCLUSIVE | `variables.expected_present`, names list |
| 5b | Ordering (marker before first request) | PASS / FAIL / INCONCLUSIVE | `ordering` timestamps and run numbers |
| 5c | Re-run safety | PASS / FAIL / INCONCLUSIVE | `bootstrap_summary` of run 2 |
| 5d | Failing pre-deploy job blocks the deploy | PASS / FAIL / INCONCLUSIVE | deployment phase and cause, `started_at` before and after, retry count |
| 6a | Injected `sslmode` | recorded value | `sslmode_in_url`, `host_has_private_prefix` |
| 6b | `sslmode=require` works | PASS / FAIL / INCONCLUSIVE | `tls[]` entry |
| 6c | `verify-full` with pasted CA works | PASS / FAIL / INCONCLUSIVE | `tls[]` entry, CA form |
| 6d | PostgreSQL server version | recorded value | `server_version` |

Also record, as findings that are not rows: spec fields that were rejected, `doctl` flags that did not exist, any behaviour that surprised you, and the cost shown on the bill after teardown.

## 10. Turning the results into updates

Do this in a documentation branch after teardown. **Never paste raw logs, URLs, tokens or certificates into the pull request.** The pull request carries the filled table from section 9 and the non-secret evidence fields, and states plainly that the results come from a throwaway test on a stated date, not from DigitalOcean documentation.

**`docs/stage-memory-platform-verification.md`** (a new status letter such as `T` for TESTED, kept separate from `V` because that file defines VERIFIED as "a DigitalOcean page says it"):

| Probe result | Update this row (change N or ambiguous to T, add date and method) |
|---|---|
| Fact 2 | 1c (path, query and body preserved) |
| Fact 3 | 1d (internal HTTPS and the service's own TLS), 1a (encryption in transit is the service's own, not the platform's), 2d (stable internal name) and add the working hostname forms |
| Fact 4 | 2c (reachable only from the same app?) and 2b (internal-only) |
| Fact 1 | 5b (`CREATE ROLE`, `GRANT`) and the "Roles" implication |
| Fact 5a, 5b, 5d | 6e (jobs reach databases), 6d (ordering), 6b (failure blocks deploy) and 6c (retry) |
| Fact 5c | the "Migration job" implication |
| Fact 6 | 3k, 4d, 4e (injected `sslmode`, CA delivery, `verify-full`), and 3g if the server version differs |
| Fact 3 variable forms | 7a, 7b, 7c (certificate and key as encrypted variables worked or did not) |

Also add a "Tested by throwaway probe" section with the table and the kit commit hash, and remove from "Open questions" the ones now answered (questions 1 to 6 and 7 partly).

**ADR-009 follow-ups** (proposals only; `docs/adr/` needs founder-reviewed merge, and governance expansion is paused, so propose edits rather than new documents):

* If fact 1 is FAIL or only works with a membership grant: replace the init-script role model on Stage with roles created through the control panel or API (`doctl databases user create`) and a GRANT-only bootstrap, recorded as a Stage divergence from the Compose init script.
* If fact 6 verify-full is FAIL: state that Stage database TLS is `require` (unauthenticated server), or switch the Stage database path to whatever verified, and record the residual risk against ADR-009's authenticated-TLS rule.
* If fact 3 is PASS: record the working internal hostname form, the certificate SAN list to use, and the one-year certificate renewal as an operational task.
* If fact 4 is FAIL (another app can reach the service): record that application-level assertion checks are the only isolation and that no second app shares the account's VPC with the memory service.
* If fact 5 ordering or failure-blocks is FAIL: the migration job cannot be relied on as a gate, and the service must refuse to serve until its schema version matches.
* Update `docs/memory-service.md` ("Stage and Production: memory is not on") only when the founder decides to build Stage memory. These results do not by themselves make Stage ready or complete any gate.

## 11. TEARDOWN (mandatory; do it the same day)

Order matters: apps first (they reference the cluster), then the trusted-source rule, then the cluster, then local files.

```text
RUN  doctl apps delete <outsider-app-id> --force
RUN  doctl apps delete <app-id> --force
RUN  doctl databases firewalls list <cluster-id>
RUN  doctl databases firewalls remove <cluster-id> --uuid <rule-uuid>     # observed working 2026-11-12; also removable in the control panel; it disappears with the cluster
RUN  doctl databases user list <cluster-id>                               # retest: note the throwaway user probe_denial (it is deleted with the cluster)
RUN  doctl databases user delete <cluster-id> probe_denial                # retest, optional if the cluster is deleted next. UNVERIFIED flags
RUN  doctl databases delete <cluster-id> --force
```

**Retest additions to this checklist (section 8b):** the throwaway database user `probe_denial` and its password file, the two scratch databases (`probe_scratch_db_a`, `probe_scratch_db_b`; the job drops them in the same run, and they vanish with the cluster, so check `doctl databases db list <cluster-id>` shows only `probe`, `defaultdb` and the platform's own databases if you are keeping the cluster for anything), the trusted-source rule, and the second app whose environment holds the throwaway credentials (deleted first, above). If you added any temporary operator `ip_addr` rule (decision N4 applies to the later conformance run, not to this retest), remove it too. Nothing in this retest should leave a resource billing after the cluster and both apps are gone.

Destroying a cluster destroys its backups (verification doc row 3f). If you pushed any image to a container registry (this kit builds from GitHub and does not need one), delete the repository and the registry, and the registry subscription if you created one for this:

```text
RUN  doctl registry repository list-v2                       # only if you used a registry
RUN  doctl registry delete --force                           # only if you created one just for the probe
```

Remove any firewall, VPC peering or trusted-source rule you added outside the above, and the project if you made one for this test.

Local cleanup (nothing generated is committed, but remove it anyway):

```text
RUN  rm -rf data/probe-cert data/probe-spec-filled.yaml data/probe-outsider-filled.yaml    # data/probe-cert includes db-denial.env from the retest
RUN  unset PROBE_TOKEN PROBE_LAPTOP_DB_PASSWORD PROBE_LAPTOP_DB_HOST PROBE_LAPTOP_DB_USER PROBE_LAPTOP_DB_PORT
```

**Verification that nothing remains billing** (every command must show no probe resource; do not skip):

```text
RUN  doctl apps list                      # no viharbot-probe and no viharbot-probe-outsider
RUN  doctl databases list                 # no probe-pg
RUN  doctl databases firewalls list <cluster-id>   # should error: cluster not found
RUN  doctl databases user list <cluster-id>        # should error: cluster not found (so probe_denial is gone)
RUN  doctl registry get                   # error or no registry, if you did not intend to keep one
RUN  doctl projects list                  # no probe project
RUN  doctl balance get                    # note month-to-date usage
```

Then, in the control panel, Billing, check the current usage after about an hour: the probe's line items must stop growing. Record the final cost next to the estimate. If any command above still lists a probe resource, delete it and re-run this list.

## 12. Maintenance notes (for whoever edits this kit)

* The kit is files only. `tests/test_digitalocean_probe.py` runs offline on loopback with a fake database and needs no DigitalOcean access.
* The probe image installs only `psycopg[binary]==3.3.8`, the version `pyproject.toml` pins for the `memory` extra. The certificate script is not in the image.
* Candidate hostnames are configurable (`PROBE_CANDIDATE_HOSTS`); the client also adds DNS search domains and non-sensitive `*_URL`, `*_DOMAIN`, `*_HOSTNAME` variables. The optional private-URL bindable variable in `probe-app.yaml` is commented out because its exact name is UNVERIFIED.
* `probe_laptop_dbcheck.py` runs on the founder's laptop and is not copied into the image (like the certificate script). It reuses `probe_outsider.database_check`, so the outsider and the laptop classify identically. Every TLS context in the kit sets `minimum_version = TLSv1_2` (a test enforces it).
* Marker table compatibility: the table keeps its original columns; the retest adds nullable `job_tag`, `attempt_id`, `started_at`, `platform_ids` with `ADD COLUMN IF NOT EXISTS`. A table from the first probe version still works (old rows group under tag `(none)`); if the columns cannot be added the job writes the old shape and reports `extended_columns: false`.
* The retest bootstrap steps are part of every job run and are idempotent; the scratch databases are dropped at the start (a previous interrupted run) and at the end of each run. Step names are listed in 8b.5 and in `tests/test_digitalocean_probe.py` (`R2_NEW_STEPS`); a test fails if this runbook stops mentioning one.
* Do not add real names, keys or passwords to any file here. Build test material at runtime in tests.
