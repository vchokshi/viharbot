# Stage memory window runbook (DigitalOcean)

**Status: a procedure written before anything exists to run it. Nothing in this document has been run.** Stage is not ready for memory. No promotion has been requested. Issue #40 stays open and no gate is complete. Every DigitalOcean action below is the founder's. The assistant never sees, generates or stores a secret, never runs `doctl`, and never contacts DigitalOcean. **Nothing here authorizes a deploy**: the only authorization is the founder's affirmative answer to the AGENTS.md question for one specific build (gate G3).

This is an ordinary runbook under `docs/`, the work item 10 of `docs/stage-memory-design.md` (section 6). It is not an ADR or a governance document. If this runbook disagrees with the code or a script, the code wins; fix this document.

**Labels.** TESTED = observed by the founder's probe on 2026-11-12 (`docs/stage-memory-platform-verification.md`, "Tested by throwaway probe"); that is observation on one account on one day. DOCUMENTED = a DigitalOcean page says it, as recorded in that file. UNVERIFIED = nobody has checked it. A name marked "(branch)" exists only in an open pull request and may change before merge.

## 1. Purpose, status and non-goals

**Purpose.** Run the governed memory service on Stage for a short "window" of a few days, backed by a DigitalOcean Managed PostgreSQL cluster created for that window and destroyed at the end (decision N1). Charlie is the only user; it stores five metadata fields per synthesis, fail-open, in its agent-private tier. See design section 1 for what memory on Stage will and will not mean.

**Non-goals.** No Production design. No other agent's memory. No high availability. No persistence across windows: **memory starts empty every window and destroying the cluster destroys its backups** (DOCUMENTED, verification row 3f). No edit to `deploy/digitalocean/app.yaml`, `AGENTS.md`, `docs/adr/`, `docs/governance/` or `.github/` by this document.

**Do not start a window until section 9 ("Not yet") is empty or the founder has accepted each open item in writing.**

## 2. Roles and approval gates

| Who | Does |
|---|---|
| Founder | Runs every `doctl` and control-panel step, generates and holds all materials and passwords (decision N2), answers the promotion question, reads the verification and signs off teardown. May instead authorize the assistant, in the founder's own words, to run **one explicit instruction at a time** with the founder's authenticated `doctl`; each authorization covers that instruction only. |
| Assistant | Writes the commands and the checklist, keeps a **non-secret resource list** (cluster ID, app ID, firewall rule UUIDs, deployment IDs, timestamps) in the window record (section 8), reads logs the founder pastes (after the founder checks them for secrets), asks the promotion question, and drives the teardown checklist. Never asks for a token, password, certificate key, CA text or database URL. |

**Gates, in order. A step may not start until the previous gate is met.**

| Gate | Met when | Evidence |
|---|---|---|
| G1 Materials prepared | The founder has generated the materials locally (step 4), stored them in the password manager, and noted the certificate fingerprint and expiry. | Fingerprint, expiry and file names in the window record. No value. |
| G2 Conformance passed | The conformance suite passed on this window's cluster before the app exists, and the operator trusted-source rule is removed and the removal verified. | Result counts, date, commit, plan, firewall list showing no operator rule. |
| G3 Promotion question answered | The assistant has posted the step 4.5 summary and the founder has answered "yes" for **that commit**. A different commit needs a new question. | The founder's answer, recorded with the commit. |
| G4 Post-deploy verification passed | Section 6 (deployment phase, health, write/restart/read drill, failure drills) all PASS. | Results table in the window record. |
| G5 Teardown verified | Section 10 verification commands show no Stage-memory resource, and the billing check after about an hour shows no growth. | Command outputs summarized, invoice figure. |

Nothing may proceed on "probably fine". A FAIL means stop, record it, and tear down if the founder asks.

## 3. Pre-window checklist (founder, a day before if possible)

1. **Merged work.** Confirm every item in section 9 is merged to `main`, or the founder accepts the gap. Write down the exact commit SHA to deploy. Run the offline suite as the README describes and record the result. Do not build or deploy from an unmerged branch.
2. **Probe retest done.** The one-time retest (decision N3) has run once and its results are recorded in `docs/stage-memory-platform-verification.md`. Section 8b of `deploy/digitalocean-probe/RUNBOOK.md` on branch `feature/stage-probe-retest` (PR #331) holds the steps R1 to R3. Until it has run, the bootstrap cannot be built (design 6, item 6).
3. **Prices and slug.** Run `doctl databases options slugs --engine pg` and use a slug from that list. `db-s-1vcpu-1gb` is no longer offered (TESTED); the probe used `db-amd-1vcpu-1gb` (15 GiB storage), whose price is **UNCONFIRMED**. Check the current price on DigitalOcean's pricing pages and in the control panel estimate before creating anything. Record the slug and price you saw.
4. **Cost estimate with arithmetic.** Sources: list prices recorded in `docs/stage-memory-platform-verification.md` (rows 3a, 3c, 8b, 8d), retrieved 2026-11-11; not read from any invoice. Database 15.15 / 672 = $0.02254 per hour (marketing page figure, slug unmatched). One extra `apps-s-1vcpu-0.5gb` memory component 5.00 / 672 = $0.00744 per hour. Bootstrap job billed only while it runs, about 0. Total about **$0.030 per hour**.

   | Window | Memory component | Database | Total |
   |---|---|---|---|
   | 24 hours | $0.18 | $0.54 | $0.72 |
   | 3 days (72 h) | $0.54 | $1.62 | $2.16 |
   | 7 days (168 h) | $1.25 | $3.79 | $5.04 |
   | forgotten for 28 days (672 h) | $5.00 | $15.15 | $20.15 |

   The database bills per started hour with a one-hour minimum (DOCUMENTED, row 3c); the conformance run before the app adds one to two such hours. Build minutes, bandwidth and extra storage are not counted (UNVERIFIED). The existing web and six agents are not counted. **If the control panel estimate or the invoice disagrees with this table, they win.**
5. **Fresh per window versus yearly.**

   | Item | Per window | Yearly |
   |---|---|---|
   | Managed cluster and dedicated database | yes (N1) | |
   | Cluster CA certificate | yes: assume a new cluster has a new CA (UNVERIFIED; the retest may record it) | |
   | Database role passwords (migrator, service) | yes | |
   | Trusted-source rules (operator, app) | yes | |
   | Memory server certificate and key, Charlie's trust copy | | yes, or a fresh certificate each window (N5) |
   | Charlie's signing key and the key registry | | yes, or whenever the certificate is replaced (see 4.3) |

6. **Reminder.** If a yearly certificate is kept, a calendar reminder at eleven months is the founder's (N5). Expiry silently turns memory off; Charlie still answers.
7. **Local workspace.** Work from a checkout of the chosen commit. All generated files and the filled spec stay under `data/` (gitignored) or in the password manager. Never put a value in shell history, a ticket, a chat or a commit message.

## 4. Step by step

Everything marked `RUN (founder)` is the founder's. Set placeholders in your own shell; do not write them into committed files. Start a window record (section 8) and write the UTC time (`date -u`) next to each step. Flag status: **TESTED** = observed working 2026-11-12 per the verification record; **UNVERIFIED** = guessed or only in a runbook; each has a control-panel fallback.

### 4.1 Create the cluster and the dedicated database

The cluster must be in the datacenter the app region maps to, in the VPC (`nyc` maps to `nyc1` and `nyc3`, TESTED).

```text
RUN (founder)  doctl apps list-regions                          # TESTED to exist; confirm the datacenter for region nyc
RUN (founder)  doctl vpcs list                                  # note the VPC UUID in that datacenter
RUN (founder)  doctl databases options slugs --engine pg        # choose a Standard single-node slug from this list
RUN (founder)  doctl databases create <cluster-name> --engine pg --version 17 --region <datacenter> --size <slug> --num-nodes 1 --private-network-uuid <vpc-uuid>
RUN (founder)  doctl databases list --format ID,Name,Status     # note the cluster ID; wait for online (about 3 minutes, TESTED)
RUN (founder)  doctl databases db create <cluster-id> <database-name>
RUN (founder)  doctl databases db list <cluster-id>
```

Status: `create` ran on 2026-11-12 and has no `--format` flag (TESTED); the other flags were used by the probe runbook but are not itemised in the record. `--format` on `list` and the `db` subcommands are UNVERIFIED. PostgreSQL 17, Standard Edition, **one node, no standby**. Control-panel fallback: Create, Databases, PostgreSQL 17, Standard, single node, the datacenter and VPC. Add the cluster ID to the resource list now.

**The cluster accepts credentialed connections from anywhere until a trusted-source rule exists (UNVERIFIED default).** It is empty and holds no real data; keep this period short.

### 4.2 Download the cluster CA

```text
RUN (founder)  doctl databases get-ca <cluster-id>              # TESTED; save the PEM to data/stage-memory/db-ca.pem
```

Control-panel fallback: the cluster, Overview, Connection details, Download CA certificate. The CA is public but still stays out of commits. It is pasted later into `MEMORY_DB_CA_PEM` (a SECRET variable; base64 or PEM both load, `src/viharbot/memory_service/stage_entry.py`, branch of PR #330). Its size was about 2 KB base64 (TESTED, 2,056 characters); variables above about 2 KB are untested.

### 4.3 Generate the materials (gate G1)

The script is `scripts/stage_memory_materials.py` (branch of PR #329). It writes only under `data/` (default `data/stage-memory/`, directory 0700, files 0600), refuses any other path, takes no secret on the command line, and prints only the certificate names, expiry, SHA-256 fingerprint, the key id and file names. The assistant must not run it and must not read its output files.

```text
RUN (founder)  python scripts/stage_memory_materials.py                    # first time: certificate, key, signing key, registry, both passwords
RUN (founder)  python scripts/stage_memory_materials.py --passwords-only   # every later window: fresh role passwords only
```

| Flag | Effect (from the script) |
|---|---|
| `--passwords-only` | regenerates only the two role passwords; leaves everything else untouched |
| `--force` | replaces certificate, key, signing key and registry together; never the passwords |
| `--force --rotate-passwords` | replaces passwords too |
| `--out <dir>` | output directory, must be under `data/` |
| `--name`, `--san`, `--days`, `--kid` | certificate name (default `memory`), extra names (avoid), validity (default 365, minimum 1), key id (default `charlie-stage-1`) |

**Fresh certificate per window (N5).** `--force` replaces the certificate, its key, Charlie's signing key and the registry as one set, so all four variables below must be re-pasted together. The passwords are untouched unless `--rotate-passwords` is given; a new cluster needs new passwords, so for a fresh-certificate window run `--force`, then `--passwords-only`.

**Where each output goes.** The script prints "paste into" lines; this is the same mapping (names from the script). All are SECRET variables unless stated.

| File under `data/stage-memory/` | Variable | Component |
|---|---|---|
| `memory.crt.b64` | `MEMORY_TLS_CERT_PEM` | `memory` |
| `memory.crt.b64` | `VIHARBOT_MEMORY_CA_PEM` | `synthesis` (Charlie's only trusted certificate) |
| `memory.key.b64` | `MEMORY_TLS_KEY_PEM` | `memory` |
| `charlie-signing.key` | `VIHARBOT_SIGNING_KEY` | `synthesis` |
| (printed key id) | `VIHARBOT_SIGNING_KID` (GENERAL) | `synthesis`, must match the registry |
| `memory-registry.b64` | `VIHARBOT_KEY_REGISTRY_B64` | `memory` |
| `memory-service-password.txt` | `MEMORY_DB_PASSWORD` | `memory` |
| `memory-service-password.txt` | `MEMORY_DB_SERVICE_PASSWORD` | `memory-bootstrap` (design 2.3; not built) |
| `memory-migrator-password.txt` | `MEMORY_DB_MIGRATOR_PASSWORD` | `memory-bootstrap` (design 2.3; not built) |
| your saved CA file | `MEMORY_DB_CA_PEM` | `memory` and `memory-bootstrap` |

Reference-only files (`memory.crt.pem`, `memory.key.pem`, `charlie-signing.pub`, `memory-registry.toml`) go in the password manager. Also set the GENERAL variables of design 2.3 (`VIHARBOT_MEMORY_URL`, `VIHARBOT_OUTBOUND_AUTH`, `VIHARBOT_SERVICE_SUBJECT`, `MEMORY_DB_HOST`, `MEMORY_DB_PORT`, `MEMORY_DB_NAME`, `MEMORY_DB_USER`, `MEMORY_STAGE_ROLE`, `PORT`). Notes where the code differs from the design text are in section 9.

**G1 is met** when the files exist, are in the password manager, and the fingerprint and expiry are in the window record.

### 4.4 Conformance run on the empty cluster (gate G2)

**Placeholder: not built.** The conformance fixture support (design 6, item 9) has not been opened as a pull request when this was written, so its variable names for the remote-cluster opt-in and the empty-cluster confirmation are not known. Do not invent them. Today the suite takes `VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN` (`tests/memory_helpers.py`), documented there as a superuser DSN for a throwaway server, and its fixture `ALTER`s the passwords of roles named `memory_migrator` and `memory_service` and drops databases with `FORCE`. **Never point the current fixture at a managed cluster.** When item 9 merges, replace this note with its real names (design 6, "Conformance run (item 9)").

The procedure, once item 9 exists, in order:

1. Confirm the cluster is the window's fresh one, before the app exists, and nothing else uses its roles.
2. Add a temporary operator rule (decision N4). The address is the founder's own and is never written to the repository.

   ```text
   RUN (founder)  doctl databases firewalls append <cluster-id> --rule ip_addr:<operator-address>     # append is TESTED; the ip_addr form is UNVERIFIED
   RUN (founder)  doctl databases firewalls list <cluster-id>                                         # note the rule UUID in the resource list
   ```

3. Run the in-scope suite from the operator machine with `verify-full` and the saved CA (names: item 9). In scope: `tests/test_memory_postgres_contract.py` except `test_records_survive_a_database_container_restart`, which needs a local Docker container. Record the result counts. PASS: every in-scope test passes and the only skip is the container-restart test (design 7.4).
4. **Remove the rule in the same session and verify.**

   ```text
   RUN (founder)  doctl databases firewalls remove <cluster-id> --uuid <operator-rule-uuid>          # TESTED
   RUN (founder)  doctl databases firewalls list <cluster-id>                                         # must show no ip_addr rule
   ```

5. From the operator machine, confirm the cluster now refuses you (the retest R1 evidence says whether this can be relied on; denial was untested until then).

**G2 is met** only with a PASS, the rule removed and the firewall list checked.

### 4.5 The promotion question (gate G3)

The assistant, not the founder's shell, asks this. AGENTS.md requires a summary and then the literal question. Use this template, filled from the window record, with no secret or ID in it.

```text
Commit: <full SHA of the build to deploy>
Verification status (each PASS or FAIL, with date):
  offline suite on this commit: ...
  local rehearsal (design 7.2): ...
  probe retest R1 to R3: ...
  conformance run on this window's cluster: ... (counts)
  drill record: not yet run (it happens after deploy)
Material cost change: about $0.030 per hour while up, <N> days = about $<X>,
  UNCONFIRMED against an invoice (slug and price per the pre-window check).
Scope in plain words: Charlie only, five metadata fields, fail-open, empty at the start
  of this window, secrets visible to anyone with console access to the app.

Do you want to promote this dev build to Stage?
```

Wait for an affirmative answer **for that commit**. "Probably", silence, or an answer about another build is not a yes. Record the answer and time.

### 4.6 Deploy

**Placeholder: not built.** The Stage app spec (design 6, item 11) is a separate pull request that needs the founder's explicit approval; `deploy/digitalocean/app.yaml` today has the web and six agents and no memory component, no job and no database binding. The bootstrap job (item 6, `MEMORY_STAGE_ROLE=bootstrap`) is not built and waits for the retest. Until both exist there is nothing to deploy, and this step cannot be carried out. When they exist, the steps are:

1. Copy the spec to `data/stage-spec-filled.yaml` (gitignored). Put the secret values into that local copy only (the platform encrypts a SECRET on first submission, DOCUMENTED row 7a; creating an app with empty SECRET values and encrypting on first submission were not tested). Never commit it.
2. Validate and apply:

   ```text
   RUN (founder)  doctl apps spec validate data/stage-spec-filled.yaml      # TESTED to exist
   RUN (founder)  doctl apps update <app-id> --spec data/stage-spec-filled.yaml    # TESTED; the first creation uses `doctl apps create --spec ...` (TESTED)
   RUN (founder)  doctl apps list-deployments <app-id>                       # TESTED
   ```

3. **The first deployment's job runs before the app can be a trusted source.** Add the rule immediately after the app exists, then redeploy once to prove the job works with the rule in place.

   ```text
   RUN (founder)  doctl databases firewalls append <cluster-id> --rule app:<app-id>      # TESTED
   RUN (founder)  doctl databases firewalls list <cluster-id>                            # note the rule UUID
   RUN (founder)  doctl apps create-deployment <app-id>                                  # TESTED
   RUN (founder)  doctl apps list-deployments <app-id>                                   # wait for ACTIVE
   ```

   If the first deployment's job failed, expected when trusted sources were already enabled on the cluster (UNVERIFIED), a failing job blocks the deploy and the platform starts an automated rollback that finishes in seconds (TESTED). Add the rule, redeploy, and record both outcomes.
4. `PORT` for `memory` must be `8443`. The image defaults `PORT` to 8000 (`Dockerfile.memory`, `ENV PORT`), and the launcher only falls back to 8443 when `PORT` is unset, so the spec must set it (`stage_entry.py`, PR #330).
5. Charlie's memory variables take effect only on a new deployment. Memory is active whenever `VIHARBOT_MEMORY_URL` is set; there is no switch.

## 5. Reading audit lines (decision N7)

Audit lines stay in runtime logs and are not forwarded. Runtime logs have **no stated retention** unless forwarded (DOCUMENTED, row 10a), so copy what the window record needs while it is up.

```text
RUN (founder)  doctl apps logs <app-id> memory --type run        # memory service: audit lines, startup lines
RUN (founder)  doctl apps logs <app-id> synthesis --type run     # Charlie: its "memory client" lines
RUN (founder)  doctl apps logs <app-id> memory-bootstrap --type deploy     # the job (placeholder name from design; not built)
```

`--type run` and `--type deploy` for jobs are TESTED. `--deployment <id>` is refused for superseded deployments (HTTP 400, TESTED); read the newest deployment's logs while it is newest.

**Memory service audit line** (`src/viharbot/memory_service/audit.py`, formatter `src/viharbot/observability.py`): one JSON object per decision, event text `memory audit`.

| Field | Meaning |
|---|---|
| `timestamp`, `level` | UTC time; `info` for `ok` and `replayed`, `warning` for everything else |
| `agent` | always `memory` |
| `request_id` | request identifier Charlie sent (links to Charlie's line) |
| `caller` | verified subject (`charlie`), or null when authentication failed |
| `scope` | tier and scope key, for Charlie `agent/charlie`; `unparsed` or `reserved` for bad paths |
| `operation` | `create`, `list`, `read`, `update`, `classify`, `retire`, `erase`, or the lower-case HTTP method for a denial |
| `outcome` | `ok`, `replayed` (idempotent repeat), `denied`, `invalid`, `not_found`, `conflict`, `error` |
| `reason` | fixed code, present on refusals only (for example `authentication_failed`, `invalid_body`, `storage_unavailable`) |
| `memory_id` | record id, where there is one |

Other fields in the line (`destination_agent`, `model_alias`, `duration_ms`) are null for audit lines. **No question, answer, finding, token, key or request body appears**; the audit call has no parameter that could carry content (`docs/memory-service.md`, audit section). Read a sample before you paste anything anywhere, as the app's environment can contain things this runbook does not know about.

**Charlie's line**, event `memory client` (`src/viharbot/memory_client.py`, PR #329): `agent`, `request_id`, `destination_agent` (`memory`), `operation`, `decision` (`ok` or a refusal), `reason` and `duration_ms`. Reasons seen in the drills: `unreachable`, `timeout`, `http_<status>`, `tls_verification_failed`, `denied`, `bad_ca_pem`, `ca_conflict`.

**Memory launcher start lines** (PR #330) are JSON with an `event` field: `memory_stage_tls_certificate` (`days_to_expiry`, certificate `fingerprint_sha256`), `memory_stage_tls_certificate_expiring` (a warning, under 30 days, `expired` true if past), `memory_stage_start` (`port`, `database_tls` = `verify-full`). A refusal prints `memory-stage: refusing to start: <fixed text>` naming at most a variable, never a value.

Not available yet: a `count` number on `list` lines (design 6, item 8). Until then a restart-and-read drill is judged from `list` with outcome `ok`, plus Charlie's `memory client` line `decision` `ok`.

## 6. Verification (gate G4)

All four parts must PASS. The assistant may read logs the founder shares; the founder reads the result.

### 6.1 Deployment phase and components

```text
RUN (founder)  doctl apps list-deployments <app-id>      # newest deployment phase ACTIVE; note its ID
RUN (founder)  doctl apps get <app-id>                   # UNVERIFIED output; every component running, including memory
```

PASS: newest deployment ACTIVE, the bootstrap job succeeded (its log line present), `memory` and `synthesis` running. A job failure shows as ERROR, the old version keeps serving and a rollback follows (TESTED).

### 6.2 Memory health: the response, not just "running"

`memory` has no public route. The platform check is a TCP check (design 2.4), so **a component can be running while the service is unhealthy**. The service's own `/health` returns `{"status":"ok","agent":"memory"}` with 200 only when its database ping works, and `{"status":"unavailable","agent":"memory"}` with 503 when it does not (`src/viharbot/memory_service/app.py`). A wrong cluster CA gives 503 while the component still runs. Check the response in one of these ways:

1. **Console, UNVERIFIED** (not known to work for a Dockerfile component): open a console on `synthesis` (`doctl apps console <app-id> synthesis`, UNVERIFIED) and run a one-off request that prints the status and body. It uses only the public certificate already in that component's environment and prints no secret.

   ```text
   python3 - <<'PY'
   import os, ssl, base64, urllib.request as u, urllib.error as e
   pem = base64.b64decode(os.environ["VIHARBOT_MEMORY_CA_PEM"]).decode()
   ctx = ssl.create_default_context(cadata=pem)
   try:
       r = u.urlopen("https://memory:8443/health", context=ctx, timeout=5)
       print(r.status, r.read().decode())
   except e.HTTPError as err:
       print(err.code, err.read().decode())
   PY
   ```
2. **Indirect, always available:** the write in 6.3 produces `operation` `create`, `outcome` `ok` in the memory log, which can only happen if the database write worked, and Charlie's `memory client` line shows `decision` `ok`. If instead Charlie's reason is `http_503`, the service is up and its database is not.

PASS: a 200 `ok` response (way 1) or a `create ... ok` line (way 2). FAIL: 503, no line, or any content in a log.

### 6.3 Charlie write, restart, read drill

1. Send one synthesis request through the normal route. Expect memory log `operation` `create`, `outcome` `ok`, `scope` `agent/charlie`, and Charlie's `memory client` `decision` `ok`.
2. Restart everything (a manual deployment restarts all components, TESTED): `doctl apps create-deployment <app-id>`; wait for ACTIVE.
3. Send a second request. Expect `operation` `list`, `outcome` `ok` and a Charlie line with `decision` `ok`. The `count` check (item 8) is not available.

PASS: both lines present, no `denied` or `error`, no prompt, answer or token in any log. This is the managed-cluster equivalent of the local test that needs a Docker container.

### 6.4 Failure drills (Charlie must still answer every time)

Do one at a time. Restore the correct value and redeploy between drills. Drill materials come from a separate generator run (`--out data/stage-memory-drill`), never from the live directory.

| Drill | How (founder) | PASS | FAIL |
|---|---|---|---|
| Memory down | set a wrong `MEMORY_DB_PASSWORD` on `memory` in the filled spec copy, redeploy | Charlie answers; one content-free `memory client` line, `reason` `http_503` (or `unreachable`, `timeout`); memory `/health` 503 | Charlie errors or hangs past its time bound |
| Wrong certificate | paste `memory.crt.b64` from the drill directory into Charlie's `VIHARBOT_MEMORY_CA_PEM`, redeploy `synthesis` | answers; `reason` `tls_verification_failed`; no request sent | any request sent to an unverified server |
| Expired certificate | see note below | same as above | same |
| Wrong signing key | paste the drill `charlie-signing.key` into Charlie's `VIHARBOT_SIGNING_KEY`, keep the registry unchanged | answers; memory audit `denied` with `reason` `authentication_failed`; Charlie `reason` `denied` | a record written by the wrong key |
| Database unreachable | `doctl databases firewalls remove <cluster-id> --uuid <app-rule-uuid>` (TESTED), then restore with `append --rule app:<app-id>` | memory `/health` 503; Charlie answers with `http_503` or `timeout` | Charlie fails |

**Expired-certificate note.** `scripts/stage_memory_materials.py --days` accepts 1 to 3650 only, so it cannot produce a certificate whose `notAfter` is already past. The design's "past notAfter" drill therefore needs another way to make one (for example a one-off local script outside the repository by the founder) or is recorded as SKIPPED with the reason. Do not hand-edit the live materials for it. Both `memory` and Charlie must hold the same expired certificate for the failure to be a clock failure rather than a wrong-certificate failure.

After the drills, restore the live values, redeploy, and re-run 6.3 once to show memory works again.

**G4 is met** when 6.1 to 6.4 PASS and are in the window record. Any FAIL: stop and tell the founder; do not describe the build as verified.

## 7. Usage during the window

The window lasts the agreed days. The founder uses Stage normally; Charlie stores five metadata fields per synthesis, nothing else. Spot-check logs once a day: the `memory_stage_tls_certificate` line's `days_to_expiry`, no `error` outcomes, no content. Note any surprise in the window record. Do not add other agents to memory or store anything sensitive: backups last 7 days and there is no selective deletion, and secrets are visible to console users (design section 10). Do not extend the window silently; if it must run longer, update the cost line and tell the founder.

## 8. Records to keep after each window

Write this in a private note kept by the founder; do not paste logs wholesale and never a secret, ID of the founder's app, URL or address.

```text
Window record (non-secret)
Date range (UTC), who ran it: ______
Commit deployed (full SHA): ______    Merged PRs included: ______
Cluster: slug ______  PostgreSQL version ______  region ______  nodes 1  edition Standard
Price seen at creation (source): ______  CA differed from last window: yes / no / unknown
Materials: certificate fresh this window or reused (expiry ______), fingerprint ______ (prefix only)
G1 ______  G2 ______  G3 answered yes at ______  G4 ______  G5 ______
Conformance: total ___  passed ___  failed ___  skipped ___ (only the container-restart test)
Deploy: first job outcome ______  after rule ______  deployment phases ______
Health: 200 / 503, how checked ______
Write-restart-read: create ok ___  list ok ___
Drills: memory down ___  wrong certificate ___  expired ___ (or SKIPPED)  wrong key ___  database unreachable ___
Surprises, rejected spec fields, doctl flags that did not exist: ______
Teardown completed at ______ ; nothing-left-billing check ______ ; billing after ~1 hour ______
Cost from the invoice: $______ vs estimate $______
```

Keep it as a short results section the founder chooses (an issue comment on #40 is acceptable; it must not claim a gate complete) and add what the founder wants kept to `docs/stage-memory-platform-verification.md` in a separate documentation pull request. If the invoice disagrees with section 3, record the invoice and correct the prices.

## 9. Not yet (work items not built or not merged when this was written)

Re-check each against `main` before a window. When one changes, edit the matching step above.

| Item (design section 6) | State when written | Affects |
|---|---|---|
| 1, 2, 4 Memory launcher: TLS and registry from variables, `verify-full` with a CA from a variable | open draft PR #330, branch `feature/stage-memory-launcher` | 4.3, 4.6, 5. Launcher accepts only `MEMORY_STAGE_ROLE=serve`; other values exit 2. |
| 3 Charlie trusts a certificate from a variable | open draft PR #329 | 4.3, 6.4 |
| 7 Stage material generator | open draft PR #329 (`scripts/stage_memory_materials.py`) | 4.3 |
| 5 Migration runner skips `CREATE SCHEMA` when it exists | open draft PR #328, branch `fix/memory-migrate-existing-schema` | bootstrap (not built) |
| Probe retest R1 to R3 | kit changes in open draft PR #331; **not run** | pre-window check 2; trusted-source denial and the migrator grants are still unproven |
| 6 Bootstrap and migrate entry (`MEMORY_STAGE_ROLE=bootstrap`) | **not built**; waits for the retest (N3) | 4.6, 5; no way yet to create the two roles |
| 8 `count` on the list audit line | **not built** | 5, 6.3 |
| 9 Conformance support against a managed cluster | **not opened**; variable names for the remote-cluster opt-in and the empty-cluster confirmation unknown | 4.4 |
| 11 Stage app spec with `memory`, `memory-bootstrap`, `memory-db`, Charlie's variables | **not built**; needs the founder's explicit approval; `deploy/digitalocean/app.yaml` unchanged | 4.6 |
| 12 Documentation updates (`docs/memory-service.md`, `docs/deployment-strategy.md`, `docs/development-status.md`) | **not built** | none |
| Local rehearsal (design 7.2) | **not run** | G3 evidence |
| AGENTS.md sentence "Stage currently has the web service and six agents" | stale once item 11 deploys; founder-reviewed path, not edited here | none |

Places where the code differs from the design text (the code's name is used above):

| Design says | Code says |
|---|---|
| Client uses `ssl.create_default_context(cadata=...)` (5.3) | PR #329 builds a client context and loads only the supplied certificate; both `VIHARBOT_MEMORY_CA_FILE` and `VIHARBOT_MEMORY_CA_PEM` set and different gives reason `ca_conflict`; bad PEM gives `bad_ca_pem` |
| `MEMORY_DB_SSLMODE` must be set to `verify-full` (2.3) | with the launcher the mode is forced to `verify-full` when a CA is given; a non-loopback host without `MEMORY_DB_CA_PEM` is refused (PR #330) |
| The launcher serves `serve` and `bootstrap` (item 6 "one image, role by variable") | PR #330 implements only `serve`; the image runs the launcher whenever `MEMORY_STAGE_ROLE` is non-empty |
| Fresh materials per window (N2, N5) | the script's `--passwords-only` changes passwords only; a new certificate means `--force`, which also replaces the signing key and registry |

## 10. Teardown and nothing-left-billing checklist (gate G5)

Modeled on `deploy/digitalocean-probe/RUNBOOK.md` section 11, which the founder ran on 2026-11-12. Per decision N1, **the cluster is destroyed every window**. **Destroying the cluster destroys its backups, and memory starts empty at the next window.** Order: app (or memory pieces), then trusted-source rules, then the cluster, then local files.

**Option A: delete the whole `viharbot-stage` app** (the other seven components go too; use this only if the founder wants all of Stage off).

```text
RUN (founder)  doctl apps delete <app-id> --force               # TESTED
```

**Option B: keep the web and six agents; remove only the memory pieces.** Placeholder: needs the item 11 spec. Edit your local spec copy to remove the `memory` service, the `memory-bootstrap` job and the `memory-db` binding, remove Charlie's memory variables (above all `VIHARBOT_MEMORY_URL`, since memory is on whenever it is set), validate, apply with `doctl apps update <app-id> --spec data/stage-spec-filled.yaml`, and wait for ACTIVE. Then Charlie logs no memory call.

Then, in both options:

```text
RUN (founder)  doctl databases firewalls list <cluster-id>                         # note every rule, including any operator-address rule
RUN (founder)  doctl databases firewalls remove <cluster-id> --uuid <rule-uuid>     # TESTED; once per rule
RUN (founder)  doctl databases delete <cluster-id> --force                          # destroys its backups
RUN (founder)  rm -rf data/stage-spec-filled.yaml data/stage-memory-drill
```

Keep `data/stage-memory/` only if the founder keeps the yearly certificate, signing key and registry; the database passwords and `db-ca.pem` are always deleted (the passwords belong to a cluster that no longer exists). Delete any copy of a value pasted into a note or password manager entry that was only for this window.

**Verify nothing remains billing.** Every command must show no Stage-memory resource:

```text
RUN (founder)  doctl apps list                                      # no memory components (or no viharbot-stage, for option A)
RUN (founder)  doctl databases list                                 # no cluster for this window
RUN (founder)  doctl databases firewalls list <cluster-id>          # should error: not found
RUN (founder)  doctl registry get                                   # error or no registry (none should have been created)
RUN (founder)  doctl balance get                                    # note month-to-date usage
```

Also confirm no operator-address rule remains and no VPC, project or registry was added. In the control panel, check Billing after about an hour: the line items for the cluster and the memory component must stop growing. Record the final cost next to the estimate (section 8). If any command still lists a resource, delete it and run the list again.

**G5 is met** when the five commands are clean, the billing check is done and the window record is written.

## 11. Failure handling

How to confirm without exposing secrets: read fixed codes and names in logs; never paste a variable value or a connection string; compare certificate **fingerprints** (the script prints one, the launcher logs `fingerprint_sha256`) and never contents.

| What went wrong | Symptom | Confirm | Remedy |
|---|---|---|---|
| Bootstrap job fails | deployment ERROR, old version keeps serving, automated rollback follows (TESTED) | `doctl apps logs <app-id> memory-bootstrap --type deploy`; read the SQLSTATE and fixed text only | correct the cause below, redeploy; steps are idempotent so a re-run is safe |
| Certificate or key pasted wrongly | `memory` exits at start with `memory-stage: refusing to start: MEMORY_TLS_KEY_PEM does not match MEMORY_TLS_CERT_PEM` or "does not parse" | the refusal line names a variable only | re-paste the base64 file (`.b64`); base64 survives the control panel, raw PEM may be flattened |
| Wrong cluster CA | `memory` runs, `/health` 503, Charlie reason `http_503`; platform shows the component healthy (TCP check) | 6.2 response, not the component state | re-save the CA from `doctl databases get-ca` for this cluster; a new cluster has its own CA until shown otherwise |
| Trusted-source rule missing | job or memory cannot connect to the database; first deployment fails (design 4) | `doctl databases firewalls list <cluster-id>` shows no `app:` rule | `append --rule app:<app-id>`, then `doctl apps create-deployment <app-id>` |
| Operator rule left behind | firewall list shows an `ip_addr` rule | the same list | `remove --uuid`; check section 10 |
| Variable mangled by the control panel | refusal lines like "is neither PEM text nor base64 of PEM" or "has no keys" | the refusal names the variable | paste the base64 `.b64` file; avoid editing a PEM in the browser |
| Port mismatch | component fails the TCP health check or never becomes healthy; `memory_stage_start` shows `port` 8000 | the `memory_stage_start` line | set `PORT` to `8443`; `internal_ports` and the health check must also say 8443 |
| Health 503 | 6.2 returns 503 or Charlie logs `http_503` | the memory log for a database error class (class only, no text); check CA, passwords, rule | fix whichever of CA, `MEMORY_DB_PASSWORD` or rule is wrong, redeploy `memory` |
| Charlie reason `denied` | memory audit `denied`, `authentication_failed` | key id: `VIHARBOT_SIGNING_KID` against the registry's key id (the script prints it); fingerprints | re-paste matching `charlie-signing.key` and `memory-registry.b64` from one generator run |
| Charlie reason `tls_verification_failed` | memory off, Charlie answers | compare certificate fingerprint printed by the script with the launcher's `fingerprint_sha256` | re-paste the same `memory.crt.b64` into both components; check expiry (`days_to_expiry`) |
| Database password mismatch after redeploy | `/health` 503 after `--passwords-only` | passwords for `memory` and the bootstrap come from the same file | set `MEMORY_DB_PASSWORD` equal to `MEMORY_DB_SERVICE_PASSWORD`, redeploy |
| `doctl` flag rejected | usage error | `doctl <command> --help` | use the control-panel fallback and record the flag in the window record |
| Anything content-like in a log | a prompt, answer or token appears | stop; do not copy it | tell the founder; this is a FAIL of G4 and a defect to report, not to work around |

If a step fails in a way this table does not cover, record the exact fixed text and the step, tear down if in doubt, and do not improvise around a security check (never switch a connection to `require` or disable hostname verification).
