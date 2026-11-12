# DigitalOcean probe runbook (throwaway)

**Status:** a test kit and a procedure. Nothing in it has been run. The author of this kit did not provision, deploy or contact DigitalOcean, used no `doctl`, account, token or credential, and did not run any command below. **Every `doctl` command and control-panel step here is something the founder runs by hand.** Every claim about how DigitalOcean behaves is **UNVERIFIED** until the procedure produces the evidence for it. Implementation under human supervision; nothing here merges or approves anything.

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
* The kit's branch pushed to GitHub and readable by App Platform's GitHub integration (the specs build from `vchokshi/viharbot`, branch `feature/stage-probe-kit`; change `branch` to `main` after merge). Docker is **not** required locally: App Platform builds `deploy/digitalocean-probe/Dockerfile.probe`.
* Python 3.12+ with `cryptography` (already a ViharBot dependency) to run the certificate script, `curl`, and `jq` (optional, for reading results).
* A scratch shell on your laptop. Keep the throwaway values in files under the repository's gitignored `data/` directory (or outside the repository), never in shell history or a ticket.
* Time: about 60 to 90 minutes for one pass. Plan to finish and tear down in one sitting.

## 3. Estimated cost

**All prices below are the list prices already recorded in `docs/stage-memory-platform-verification.md` (retrieved 2026-11-11). They are UNCONFIRMED: confirm current prices on DigitalOcean's pricing pages and in the control panel's cost estimate before you create anything.** I cite no other price.

| Item | Recorded list price | Hourly equivalent (720-hour month assumed) |
|---|---|---|
| App Platform `apps-s-1vcpu-0.5gb` (shared, 512 MiB) | $5.00 per month (verification doc row 8b) | 5.00 / 720 = $0.00694 |
| Standard single-node PostgreSQL, 1 GiB | $15.00 per month (docs pricing page) or $15.15 per month, $0.02254 per hour (marketing page) (row 3a) | use the higher, cited hourly figure: $0.02254 |
| Pre-deploy job | billed only while it runs (row 8d), seconds per run | about 0 |

Components: probe-service, probe-client and probe-outsider are three always-on components of $5.00 per month each: 3 x 0.00694 = $0.02083 per hour. Database: $0.02254 per hour. Total: 0.02083 + 0.02254 = **$0.04337 per hour**. For a 4-hour window: 4 x 0.04337 = **$0.17**. For 8 hours: **$0.35**. If you forgot everything for a full day: 24 x 0.04337 = **$1.04**. Databases are billed by the hour with a one-hour minimum and services per second with a one-minute minimum (rows 3c, 8b), so a 20-minute test of the database still costs one hour. Not counted, UNVERIFIED: build minutes, bandwidth, and any charge for the first 28 days wording in row 8b. The control panel's estimate and your invoice are the authority; if they disagree with this table, they win and this table is wrong.

Cost is not a gate here, but a forgotten cluster is the only real risk: do the teardown in section 11.

## 4. Safety rules (read before step 1)

* **Throwaway values only.** The certificate, key, results token and database are created for this test and destroyed after it. Use a new random results token. Never reuse a real ViharBot key, password or certificate.
* **Never paste a secret into an issue, pull request, chat or commit message.** That includes the contents of `data/probe-cert/`, the results token, the database URL and the CA certificate. Record only the PASS/FAIL/INCONCLUSIVE table and the non-secret evidence fields named in section 9.
* **The probe prints no secret values.** It reports variable names, redacted host shapes, SQLSTATEs and scrubbed error text. Still read the logs before you paste any of them anywhere: the app's own environment can contain things this kit does not know about.
* **Public exposure is one read-only page.** probe-client has the only public route; `/health` is open and returns `{"ok": true}`; `/results` needs the bearer token and returns names, shapes and results, never a secret. probe-service has **no** `http_port` and **no** route.
* **Until you add the trusted-source rule in step 6, the cluster accepts connections from anywhere that has valid credentials** (UNVERIFIED default). Keep that window to minutes, and the whole test to hours.
* **Destroy within hours.** The teardown is mandatory (section 11).
* The role-bootstrap steps run only in the throwaway database, creating `probe_migrator`, `probe_service` and a `probe_scratch` schema. They never touch ViharBot data (there is none).

## 5. Step by step (all commands are run by the founder)

Everything marked **RUN** is yours. `<angle brackets>` are values you substitute. Set the placeholders in your own shell; do not write them into files that get committed.

### Step 1. Choose the region and VPC, create the cluster

The cluster must be in the datacenter the app's region maps to (or a peered VPC), and only resources in the same VPC can use the private hostname (verification doc rows 4b, 3l).

```text
RUN  doctl apps list-regions                       # UNVERIFIED output columns; confirm which datacenter the app region (spec says nyc) maps to
RUN  doctl vpcs list                               # note the VPC UUID in that datacenter
RUN  doctl databases options slugs --engine pg     # confirm the Standard single-node 1 GiB slug (expected db-s-1vcpu-1gb) and that version 17 is offered
RUN  doctl databases create probe-pg --engine pg --version 17 --region <datacenter, e.g. nyc3> --size <1 GiB slug> --num-nodes 1 --private-network-uuid <vpc-uuid>
RUN  doctl databases list                          # note the cluster ID (a UUID); wait until the status is online
```

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
RUN  doctl databases get-ca <cluster-id>          # prints or saves the certificate; check `doctl databases --help`
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

Trusted sources block every other connection once enabled (row 4c), including your laptop, which is intended. UNVERIFIED: the `--rule app:<id>` form; the control panel (cluster, Settings, Trusted sources, add the app) is the documented route.

### Step 7. Run the job, read the results

The pre-deploy job ran as part of the first deployment (**run 1**). Read its output:

```text
RUN  doctl apps logs <app-id> probe-job --type deploy      # UNVERIFIED which --type shows job output; try `run` and `deploy`
```

If that prints nothing, use the control panel: the app, Activity, the deployment, the pre-deploy job's logs. Find the single line whose JSON has `"event": "probe_job_summary"`. Read the client's results (its logs and page):

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

Then set `PROBE_JOB_FAIL` back to `"false"` and deploy again (run 4) so the app is in a known state. Run 4's marker `run_number` should be at least 4: runs 1 to 3 each consumed a number, which proves run 3 executed (retries by the platform would push it higher).

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
| Job summary | pre-deploy job log, line `probe_job_summary` | variable names, redacted connection info, `tls[]`, `bootstrap[]`, `bootstrap_summary`, `marker`, `exit_code` |
| Client results | `GET /results` (token) and log line `probe_client_summary` | `hosts[]`, `echo`, `summary`, `ordering`, `pinned_certificate`, `environment_variable_names` |
| Outsider summary | outsider worker log, line `probe_outsider_summary` | `targets[]`, `public_urls[]`, `summary` |
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
| Retry behaviour (information) | number of `probe_job_summary` lines in the failed deployment's job logs | record the count (more than 1 means the platform retried) | n/a | n/a |

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
RUN  doctl databases firewalls remove <cluster-id> --uuid <rule-uuid>     # UNVERIFIED flag; also removable in the control panel; it disappears with the cluster
RUN  doctl databases delete <cluster-id> --force
```

Destroying a cluster destroys its backups (verification doc row 3f). If you pushed any image to a container registry (this kit builds from GitHub and does not need one), delete the repository and the registry, and the registry subscription if you created one for this:

```text
RUN  doctl registry repository list-v2                       # only if you used a registry
RUN  doctl registry delete --force                           # only if you created one just for the probe
```

Remove any firewall, VPC peering or trusted-source rule you added outside the above, and the project if you made one for this test.

Local cleanup (nothing generated is committed, but remove it anyway):

```text
RUN  rm -rf data/probe-cert data/probe-spec-filled.yaml
RUN  unset PROBE_TOKEN
```

**Verification that nothing remains billing** (every command must show no probe resource; do not skip):

```text
RUN  doctl apps list                      # no viharbot-probe and no viharbot-probe-outsider
RUN  doctl databases list                 # no probe-pg
RUN  doctl databases firewalls list <cluster-id>   # should error: cluster not found
RUN  doctl registry get                   # error or no registry, if you did not intend to keep one
RUN  doctl projects list                  # no probe project
RUN  doctl balance get                    # note month-to-date usage
```

Then, in the control panel, Billing, check the current usage after about an hour: the probe's line items must stop growing. Record the final cost next to the estimate. If any command above still lists a probe resource, delete it and re-run this list.

## 12. Maintenance notes (for whoever edits this kit)

* The kit is files only. `tests/test_digitalocean_probe.py` runs offline on loopback with a fake database and needs no DigitalOcean access.
* The probe image installs only `psycopg[binary]==3.3.8`, the version `pyproject.toml` pins for the `memory` extra. The certificate script is not in the image.
* Candidate hostnames are configurable (`PROBE_CANDIDATE_HOSTS`); the client also adds DNS search domains and non-sensitive `*_URL`, `*_DOMAIN`, `*_HOSTNAME` variables. The optional private-URL bindable variable in `probe-app.yaml` is commented out because its exact name is UNVERIFIED.
* Do not add real names, keys or passwords to any file here. Build test material at runtime in tests.
