# Stage memory window runbook (DigitalOcean)

**Status: run once. The first Stage window started on 2026-11-24 (see "First window record" at the end of section 4.6 and the updated section 9).** Stage memory was deployed once, from `main` commit `3bfc6c6`, after the founder answered the promotion question yes for that commit. The window is planned for 3 days from the cluster rebuild (about 2026-11-24 06:24 UTC to about 2026-11-27 06:30 UTC). Gates: G1 met, G2 met on the first cluster of the window (not re-run on the rebuilt cluster), G3 answered yes for that commit, **G4 partially met** (health, write/restart/read, and two of five failure drills passed; three drills NOT RUN), G5 not applicable yet (nothing torn down). Issue #40 stays open. Every DigitalOcean action below is the founder's. The assistant never sees, generates or stores a secret, never runs `doctl`, and never contacts DigitalOcean. **Nothing here authorizes a deploy**: the only authorization is the founder's affirmative answer to the AGENTS.md question for one specific build (gate G3), and a different commit needs a new question. Teardown happens only on the founder's explicit instruction at the end of the window.

This is an ordinary runbook under `docs/`, the work item 10 of `docs/stage-memory-design.md` (section 6). It is not an ADR or a governance document. If this runbook disagrees with the code or a script, the code wins; fix this document.

**Labels.** WINDOW = observed during the first Stage window on 2026-11-24 (one account, one day, observed by running it; not DigitalOcean documentation). TESTED = observed by the founder's probe on 2026-11-12 (`docs/stage-memory-platform-verification.md`, "Tested by throwaway probe"); that is observation on one account on one day. DOCUMENTED = a DigitalOcean page says it, as recorded in that file. UNVERIFIED = nobody has checked it. Pull requests #326 to #334 are merged to `main`; names below were checked against `main` at baseline `a84f203`.

## 1. Purpose, status and non-goals

**Purpose.** Run the governed memory service on Stage for a short "window" of a few days, backed by a DigitalOcean Managed PostgreSQL cluster created for that window and destroyed at the end (decision N1). Charlie is the only user; it stores five metadata fields per synthesis, fail-open, in its agent-private tier. See design section 1 for what memory on Stage will and will not mean.

**Non-goals.** No Production design. No other agent's memory. No high availability. No persistence across windows: **memory starts empty every window and destroying the cluster destroys its backups** (DOCUMENTED, verification row 3f). No edit to `deploy/digitalocean/app.yaml`, `AGENTS.md`, `docs/adr/`, `docs/governance/` or `.github/` by this document.

**Do not start a window until section 9 ("Not yet") lists nothing that matters to that window or the founder has accepted each open item in writing.** The first window ran with three failure drills not yet run (section 9).

**Project rule.** Every DigitalOcean resource belongs in the `viharbot` project on the My Team team. After creating anything, assign it and verify the assignment. A new database cluster was already placed in that project; `doctl apps create` put the new app in the account's default project, so assign it with `doctl projects resources assign <project-id> --resource=do:app:<app-id>` and check it.

## 2. Roles and approval gates

| Who | Does |
|---|---|
| Founder | Runs every `doctl` and control-panel step, generates and holds all materials and passwords (decision N2), answers the promotion question, reads the verification and signs off teardown. May instead authorize the assistant, in the founder's own words, to run **one explicit instruction at a time** with the founder's authenticated `doctl`; each authorization covers that instruction only. |
| Assistant | Writes the commands and the checklist, keeps a **non-secret resource list** (cluster ID, app ID, firewall rule UUIDs, deployment IDs, timestamps) in the window record (section 8), reads logs the founder pastes (after the founder checks them for secrets), asks the promotion question, and drives the teardown checklist. Never asks for a token, password, certificate key, CA text or database URL. |

**Gates, in order. A step may not start until the previous gate is met.**

| Gate | Met when | Evidence |
|---|---|---|
| G1 Materials prepared | The founder has generated the materials locally (step 4), stored them in the password manager, and noted the certificate fingerprint and expiry. | Fingerprint, expiry and file names in the window record. No value. |
| G2 Conformance passed | The conformance suite passed on this window's cluster before the app exists, and the operator trusted-source rule is removed and the removal verified. (First window: met on the window's first cluster, 82 passed, 0 failed, 1 skipped; **not re-run on the rebuilt cluster**.) | Result counts, date, commit, plan, firewall list showing no operator rule. |
| G3 Promotion question answered | The assistant has posted the step 4.5 summary and the founder has answered "yes" for **that commit**. A different commit needs a new question. | The founder's answer, recorded with the commit. |
| G4 Post-deploy verification passed | Section 6 (deployment phase, health, write/restart/read drill, failure drills) all PASS. | Results table in the window record. |
| G5 Teardown verified | Section 10 verification commands show no Stage-memory resource, and the billing check after about an hour shows no growth. | Command outputs summarized, invoice figure. |

**First window status:** G1 met (materials generated by the founder; certificate fingerprint and expiry checked). G2 met on the first cluster of the window (conformance not re-run on the rebuilt cluster). G3 answered yes for commit `3bfc6c6`. G4 partially met (see 6.4). G5 not applicable yet.

Nothing may proceed on "probably fine". A FAIL means stop, record it, and tear down if the founder asks.

## 3. Pre-window checklist (founder, a day before if possible)

1. **Merged work.** Confirm every item in section 9 is merged to `main`, or the founder accepts the gap. Write down the exact commit SHA to deploy. Run the offline suite as the README describes and record the result. Do not build or deploy from an unmerged branch.
2. **Probe retest done.** The one-time retest (decision N3) ran once on 2026-11-23 and its results are recorded in `docs/stage-memory-platform-verification.md` ("Retest 2026-11-23"). Section 8b of `deploy/digitalocean-probe/RUNBOOK.md` (merged in PR #331) holds the steps R1 to R3. Results in short: R1 trusted-source denial PASS on the public path (about 40 seconds after the rule; the private-VPC path is untested), R2 the migrator needs `GRANT CONNECT` and, to take ownership, `GRANT migrator TO CURRENT_USER WITH SET TRUE` (PASS), R3 a failing pre-deploy job is not retried but the rollback re-runs the previous version's job once (answered). This ticks this item only. (The bootstrap and the Stage app spec have since merged; see section 4.6.)
3. **Prices and slug.** Run `doctl databases options slugs --engine pg` and use a slug from that list. The offered list changes: `db-s-1vcpu-1gb` was absent on 2026-11-12 and listed again on 2026-11-23 (TESTED); the probes used `db-amd-1vcpu-1gb` (15 GiB storage), whose price is **UNCONFIRMED** (no invoice read yet). The server was PostgreSQL 17.12 on 2026-11-23. Check the current price on DigitalOcean's pricing pages and in the control panel estimate before creating anything. Record the slug and price you saw.
4. **Cost estimate with arithmetic.** Sources: list prices recorded in `docs/stage-memory-platform-verification.md` (rows 3a, 3c, 8b, 8d), retrieved 2026-11-11; not read from any invoice. Database 15.15 / 672 = $0.02254 per hour (marketing page figure, slug unmatched). One extra `apps-s-1vcpu-0.5gb` memory component 5.00 / 672 = $0.00744 per hour. Bootstrap job billed only while it runs, about 0. Total about **$0.030 per hour**.

   | Window | Memory component | Database | Total |
   |---|---|---|---|
   | 24 hours | $0.18 | $0.54 | $0.72 |
   | 3 days (72 h) | $0.54 | $1.62 | $2.16 |
   | 7 days (168 h) | $1.25 | $3.79 | $5.04 |
   | forgotten for 28 days (672 h) | $5.00 | $15.15 | $20.15 |

   **First window estimate (2026-11-24), not read from an invoice:** about $0.082 per hour for the 8 services at an assumed default $5 instance size (the spec sets no size; that default is an assumption, unconfirmed) plus about $0.0225 per hour for the database, plus model-call cost (each full request makes several live model calls; roughly ten full requests were sent during verification). For 72 hours the two hourly figures come to roughly $7.5 before model calls (arithmetic by the document's author); the invoice wins. The table below is the earlier memory-only estimate and does not include the web and agents.

   The database bills per started hour with a one-hour minimum (DOCUMENTED, row 3c); the conformance run before the app adds one to two such hours. Build minutes, bandwidth and extra storage are not counted (UNVERIFIED). The existing web and six agents are not counted. **If the control panel estimate or the invoice disagrees with this table, they win.**
5. **Fresh per window versus yearly.**

   | Item | Per window | Yearly |
   |---|---|---|
   | Managed cluster and dedicated database | yes (N1) | |
   | Cluster CA certificate | **not necessarily.** A rebuilt cluster on this account had a byte-identical CA (the SHA-256 of the CA PEM matched), so `MEMORY_DB_CA_PEM` in the app did not need updating. Observed twice on one account; do not generalise. Compare the SHA-256 of the saved CA with the new cluster's before deciding; do not assume either way. | |
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
RUN (founder)  doctl databases db create <cluster-id> <database-name>     # ORDER: do this AFTER the conformance run (4.4), not before
RUN (founder)  doctl databases db list <cluster-id>
```

**Order matters (found in the first window).** The conformance suite's empty-cluster guard refuses to run if the dedicated database already exists, because it allows only the system databases and its own `vbtest_` ones. So create the cluster, run the conformance suite (4.4), and only then create the dedicated database. If the empty dedicated database already exists, delete it first (`doctl databases db delete`).

Status: `create` ran on 2026-11-12, 2026-11-23 and 2026-11-24 and has no `--format` flag (TESTED). In `doctl` 2.0 (2026-11-23 and 2026-11-24) `databases db create` and `databases user create` also take no `--format`, `user create` and `user get` mask the password unless `--show` is given, `doctl databases connection` has no `--user` flag and masks the URI password unless `--show` is given, `databases firewalls list` takes neither `--format` nor `--no-header`, and `doctl apps list --format` accepts `ID` and `Spec.Name` but not `Region` or `ActiveDeployment.Phase` (WINDOW). **Warning: `doctl databases create` prints the admin connection URI including its password. Treat that output as a secret: do not paste it into a ticket, chat, log or this repository, and clear the terminal scrollback.** `--format` on `list` and the `db` subcommands are UNVERIFIED. PostgreSQL 17, Standard Edition, **one node, no standby**. Control-panel fallback: Create, Databases, PostgreSQL 17, Standard, single node, the datacenter and VPC. Add the cluster ID to the resource list now.

**The cluster accepts credentialed connections from anywhere while its trusted-source list is empty (WINDOW: an empty list means no restriction; TCP connects succeed from anywhere).** Refusal exists only once at least one trusted source exists. In the first window the bootstrap's first deployment succeeded while the cluster had no trusted source. The cluster is empty and holds no real data at that point; keep this period short, and add the app as a trusted source right after it exists.

### 4.2 Download the cluster CA

```text
RUN (founder)  doctl databases get-ca <cluster-id>              # TESTED; save the PEM to data/stage-memory/db-ca.pem
```

Control-panel fallback: the cluster, Overview, Connection details, Download CA certificate. The CA is public but still stays out of commits. On 2026-11-24 a rebuilt cluster returned a CA byte-identical to the first cluster's (matching SHA-256 of the PEM), so on this account a new cluster did not require updating `MEMORY_DB_CA_PEM` in the app (WINDOW; observed twice on one account, do not generalise; compare the hash rather than assume). It is pasted later into `MEMORY_DB_CA_PEM` (a SECRET variable; base64 or PEM both load, `src/viharbot/memory_service/stage_entry.py`, PR #330, merged). Its size was about 2 KB base64 (TESTED, 2,056 characters); variables above about 2 KB are untested.

### 4.3 Generate the materials (gate G1)

The script is `scripts/stage_memory_materials.py` (PR #329, merged). It writes only under `data/` (default `data/stage-memory/`, directory 0700, files 0600), refuses any other path, takes no secret on the command line, and prints only the certificate names, expiry, SHA-256 fingerprint, the key id and file names. The assistant must not run it and must not read its output files.

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
| `memory-service-password.txt` | `MEMORY_DB_SERVICE_PASSWORD` | `memory-bootstrap` |
| `memory-migrator-password.txt` | `MEMORY_DB_MIGRATOR_PASSWORD` | `memory-bootstrap` |
| your saved CA file | `MEMORY_DB_CA_PEM` | `memory` and `memory-bootstrap` |

Reference-only files (`memory.crt.pem`, `memory.key.pem`, `charlie-signing.pub`, `memory-registry.toml`) go in the password manager. Also set the GENERAL variables of design 2.3 (`VIHARBOT_MEMORY_URL`, `VIHARBOT_OUTBOUND_AUTH`, `VIHARBOT_SERVICE_SUBJECT`, `MEMORY_DB_HOST`, `MEMORY_DB_PORT`, `MEMORY_DB_NAME`, `MEMORY_DB_USER`, `MEMORY_STAGE_ROLE`, `PORT`). Notes where the code differs from the design text are in section 9.

**G1 is met** when the files exist, are in the password manager, and the fingerprint and expiry are in the window record.

### 4.4 Conformance run on the empty cluster (gate G2)

The conformance fixture support (design 6, item 9) merged in PR #334. The fixture is `tests/memory_helpers.py`; the suite is `tests/test_memory_postgres_contract.py`. It is selected by these variables (read from the module docstring and constants):

| Variable | Meaning |
|---|---|
| `VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN` | Admin DSN. Unset: the database tests skip. May be a superuser (throwaway container) or a non-superuser with CREATEROLE and CREATEDB (a managed cluster, for example `doadmin`). For TLS use `sslmode=verify-full` and `sslrootcert=<path to the CA file>`; the derived service and migrator DSNs inherit both. |
| `VIHARBOT_MEMORY_TEST_PG_ALLOW_REMOTE=1` | Required when the admin host is not loopback. |
| `VIHARBOT_MEMORY_TEST_PG_CONFIRM_EMPTY=1` | Also required for a remote host. Not taken on trust: the fixture checks the cluster and **refuses** (the run fails) if any database exists beyond the system ones (`postgres`, `template0`, `template1`, `defaultdb`, `_dodb`) and the `vbtest_` prefix, or if roles named `memory_migrator` or `memory_service` exist. |
| `VIHARBOT_MEMORY_TEST_PG_FORCE_REMOTE_GUARD=1` | Applies the remote rules to a loopback host; can only make checks stricter. |
| `VIHARBOT_MEMORY_TEST_PG_CONTAINER` | A local Docker container name, only for the container-restart test; that test skips on a remote cluster. |

Isolation: each run creates its own roles (`vbtest_m_<hex>`, `vbtest_s_<hex>`) and database (`vbtest_<hex>`) and drops only those on teardown; it does not alter the real `memory_migrator`, `memory_service` or their database. This is why the guard above must pass on a fresh cluster. Still run it only against the window's own empty cluster, never one that holds live data.

The procedure, in order:

1. Confirm the cluster is the window's fresh one, before the app exists, and nothing else uses its roles. **The dedicated database from 4.1 must not exist yet** (the guard refuses to run otherwise; create it after this run).
2. Add a temporary operator rule (decision N4). The address is the founder's own and is never written to the repository.

   ```text
   RUN (founder)  doctl databases firewalls append <cluster-id> --rule ip_addr:<operator-address>     # append and the ip_addr form WORK (WINDOW, 2026-11-24)
   RUN (founder)  doctl databases firewalls list <cluster-id>                                         # note the rule UUID in the resource list
   ```

3. Run the in-scope suite from the operator machine with `verify-full` and the saved CA, using the variables above. In scope: `tests/test_memory_postgres_contract.py` except `test_records_survive_a_database_container_restart`, which needs a local Docker container. Record the result counts. PASS: every in-scope test passes and the only skip is the container-restart test (design 7.4).

   **Duration and timeout (WINDOW).** Against a remote cluster the suite takes about 11 minutes (first window: 11 minutes 22 seconds). A 9-minute timeout killed one attempt and left `vbtest_` databases and roles on the cluster. Allow at least 15 minutes. If a run is killed, remove the leftovers with `doctl` before anything else: list with `doctl databases db list <cluster-id>` and `doctl databases user list <cluster-id>`, then `doctl databases db delete <cluster-id> <name>` and `doctl databases user delete <cluster-id> <name>` for each `vbtest_` entry.

   **First window result (WINDOW; first cluster of the window; `verify-full` with the saved CA; the three variables above set):** 82 passed, 0 failed, 1 skipped (the container-restart test), 11 minutes 22 seconds. It was **not re-run on the rebuilt cluster**.
4. **Remove the rule in the same session and verify.**

   ```text
   RUN (founder)  doctl databases firewalls remove <cluster-id> --uuid <operator-rule-uuid>          # TESTED; worked again in the first window
   RUN (founder)  doctl databases firewalls list <cluster-id>                                         # must show no ip_addr rule (this command takes no --format or --no-header)
   ```

5. **Do not expect a refusal yet.** An earlier version of this step said the cluster should now refuse the operator machine. That is wrong for an empty trusted-source list: with the operator rule removed the list is empty again, and an empty list means **no restriction** (TCP connects succeed from anywhere; WINDOW). Refusal exists only once at least one trusted source exists; the retest saw refusal about 40 seconds after a rule was added (R1, 2026-11-23, and again in the first window). The check that the operator rule is gone is the firewall list, not a refused connection. A refusal check is meaningful only after the app's rule (4.6 step 3) exists.

**G2 is met** only with a PASS, the rule removed and the firewall list checked. In the first window the operator rule was removed with `remove --uuid` and the list was checked.

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

**Done once, on 2026-11-24, from `main` commit `3bfc6c6`** (the founder answered the promotion question yes for that commit; G3). The bootstrap (#354) and the Stage app spec (#356) are merged; the bootstrap fix for non-superuser admins is PR #361. The steps are kept below for the next window, with what the first deployment showed.

1. Copy the spec to `data/stage-spec-filled.yaml` (gitignored). Put the secret values into that local copy only. In the first window the app `viharbot-stage` (web, six agents, the memory service and the `PRE_DEPLOY` memory-bootstrap job) was **created** from a locally filled spec with secrets pasted into the gitignored copy, and the platform encrypted the SECRET values on first submission (this worked with values supplied at creation; WINDOW; verification row 7a). Never commit the filled copy. Creating an app with empty SECRET values was still not tested.
2. Validate and create or apply:

   ```text
   RUN (founder)  doctl apps spec validate data/stage-spec-filled.yaml      # TESTED to exist
   RUN (founder)  doctl apps create --spec data/stage-spec-filled.yaml      # used for the first window
   RUN (founder)  doctl apps update <app-id> --spec data/stage-spec-filled.yaml    # later changes
   RUN (founder)  doctl apps list-deployments <app-id>                       # TESTED
   ```

   `doctl apps create` assigns the app to the account's default project; assign it to the `viharbot` project and verify (project rule, section 1).

   **Whole-spec overwrite caution (WINDOW).** A full-spec `doctl apps update` replaces the entire spec, including any change made in the console since the last deployment. In the first window the founder edited a non-secret variable in the console, and a later full-spec update from the committed file reverted it. Before applying, diff the live spec against the file: `doctl apps spec get <app-id>` and `doctl apps get-deployment <app-id> <deployment-id> -o json`. Secret values in a diff are masked, so **a console change to a SECRET variable would be invisible in the diff**; expect that blind spot and, if a secret may have been edited in the console, re-enter it in the file deliberately.
3. **The first deployment's job runs before the app can be a trusted source.** Add the rule immediately after the app exists, then redeploy once to prove the job works with the rule in place.

   ```text
   RUN (founder)  doctl databases firewalls append <cluster-id> --rule app:<app-id>      # TESTED
   RUN (founder)  doctl databases firewalls list <cluster-id>                            # note the rule UUID
   RUN (founder)  doctl apps create-deployment <app-id>                                  # TESTED
   RUN (founder)  doctl apps list-deployments <app-id>                                   # wait for ACTIVE
   ```

   In the first window the first deployment's job succeeded **because the cluster had no trusted source** (an empty list is unrestricted, 4.1). Had a trusted source already existed without the app, the job would have failed and the platform would have started an automated rollback (TESTED, probe). The app was added as the cluster's only trusted source right after it existed and redeployed once; all fine.
4. `PORT` for `memory` must be `8443`. The image defaults `PORT` to 8000 (`Dockerfile.memory`, `ENV PORT`), and the launcher only falls back to 8443 when `PORT` is unset, so the spec must set it (`stage_entry.py`, PR #330, merged).
5. Charlie's memory variables take effect only on a new deployment. Memory is active whenever `VIHARBOT_MEMORY_URL` is set; there is no switch.

**Bootstrap, first window (WINDOW).**

- First run on an empty cluster: 12 steps, all ok, with `verify-full`: create_role (client-side SCRAM hashing works on DigitalOcean), grants, schema, migrations 0001 and 0002 applied, service login ok.
- Second run (the app being the only trusted source): all 12 ok, the `alter_role` steps ok, migrate applied nothing (idempotent).
- The bindable names `${memory-db.HOSTNAME}`, `${memory-db.PORT}`, `${memory-db.USERNAME}` and `${memory-db.DATABASE}` resolved in the job and in the service and worked (the `DATABASE` binding is now covered by a test).
- **A defect found first by the local rehearsal:** on PostgreSQL 16 and later, a non-superuser admin cannot run `ALTER ROLE` naming `SUPERUSER`, even as `NOSUPERUSER` (SQLSTATE 42501). It made the bootstrap's second run fail locally. PR #361 stopped naming `SUPERUSER` in `ALTER ROLE`. It matters because DigitalOcean's automated rollback re-runs the job (R3), so an unfixed bootstrap would have failed on any rollback or retry.

**Incidents in the first window.**

- The first memory cluster was deleted in the DigitalOcean console by the founder (about 06:17 to 06:20 UTC on 2026-11-24) while the app was running. The memory service logged connect failures and Charlie degraded fail-open as designed (the request still returned HTTP 200 and Charlie skipped memory with `http_503`). The cluster was rebuilt (created about 06:24 UTC; the trusted-source rule was added before anything connected; redeployed; the bootstrap's first-run path on the empty cluster passed again). The conformance run was **not** repeated on the rebuilt cluster.
- Whole-spec overwrite: see the caution in step 2.

**First window record (non-secret).** Commit `3bfc6c6`. Cluster: Standard, PostgreSQL 17, single node, slug `db-amd-1vcpu-1gb`, region `nyc1`, default VPC, in the `viharbot` project. Deploy: first job ok (no trusted source existed), after the rule ok, one redeploy ok. Conformance: 82 passed, 0 failed, 1 skipped (first cluster only). Health and drills: sections 6.1 to 6.4. Teardown: not done (founder's instruction only). Cost: not read from an invoice (section 3, item 4).

## 5. Reading audit lines (decision N7)

Audit lines stay in runtime logs and are not forwarded. Runtime logs have **no stated retention** unless forwarded (DOCUMENTED, row 10a), so copy what the window record needs while it is up.

```text
RUN (founder)  doctl apps logs <app-id> memory --type run        # memory service: audit lines, startup lines
RUN (founder)  doctl apps logs <app-id> synthesis --type run     # Charlie: its "memory client" lines
RUN (founder)  doctl apps logs <app-id> memory-bootstrap --type deploy     # the job
```

**The memory service logs no `/health` lines and has no public route** (WINDOW). The platform health check is a TCP check, so the service's `/health` cannot be probed from outside, and nothing in the log shows a health request. Its proof of life is its audit lines. The six agents' `/health` answers about every 10 seconds appear in their own logs; the web's `/health` is 200 publicly, the web needs basic auth (401 without credentials), and the memory route is not reachable from outside (404 on the public hostname).

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

**Charlie's line**, event `memory client` (`src/viharbot/memory_client.py`, PR #329, merged): `agent`, `request_id`, `destination_agent` (`memory`), `operation`, `decision` (`ok` or a refusal), `reason` and `duration_ms`. Reasons seen in the drills: `unreachable`, `timeout`, `http_<status>`, `tls_verification_failed`, `denied`, `bad_ca_pem`, `ca_conflict`.

**Memory launcher start lines** (PR #330, merged) are JSON with an `event` field: `memory_stage_tls_certificate` (`days_to_expiry`, certificate `fingerprint_sha256`), `memory_stage_tls_certificate_expiring` (a warning, under 30 days, `expired` true if past), `memory_stage_start` (`port`, `database_tls` = `verify-full`). A refusal prints `memory-stage: refusing to start: <fixed text>` naming at most a variable, never a value.

A successful `list` line carries a `count` of the records returned (design 6, item 8; PR #333, merged). A restart-and-read drill is judged from a `list` line with outcome `ok` and `count` of at least 1, plus Charlie's `memory client` line `decision` `ok`.

## 6. Verification (gate G4)

All four parts must PASS. The assistant may read logs the founder shares; the founder reads the result. **First window status (2026-11-24): G4 is partially met.** 6.1 and 6.2 passed (by way 2), 6.3 passed, and in 6.4 two of five drills passed and three are NOT RUN.

### 6.1 Deployment phase and components

```text
RUN (founder)  doctl apps list-deployments <app-id>      # newest deployment phase ACTIVE; note its ID
RUN (founder)  doctl apps get <app-id>                   # UNVERIFIED output; every component running, including memory
```

PASS: newest deployment ACTIVE, the bootstrap job succeeded (its log line present), `memory` and `synthesis` running. First window: passed; all six agents answered `/health` about every 10 seconds, and web `/health` was 200 publicly. A job failure shows as ERROR, the old version keeps serving and a rollback follows (TESTED).

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

First window: the memory service has no public route, its platform check is TCP, and it logs no `/health` lines, so its `/health` could not be probed from outside and way 1 (the console one-off) was not run. Way 2 passed (`create` `ok`, below). Do not describe the memory service's own `/health` as observed.

**Live-team limitation (WINDOW).** Stage web now sets `CHARLIE_PLANNER_MODEL` and holds a LiteLLM key, so `/api/chat` uses the iterative live team conversation, not a fixed pipeline. Memory is exercised only when Charlie's planner schedules the synthesis capability. Of the requests sent, two (a short question and an open-ended question) completed with research, security and source-quality steps and Charlie approving without calling the synthesis service, so no memory read or write happened. Requests that explicitly asked for research, a security assessment, a privacy review and a synthesis ran synthesis and used memory every time. The memory feature is wired in the synthesis service, not in the planner; **deterministic memory use needs a path that always calls synthesis.** A verification request must therefore ask for a synthesis explicitly, and a quiet memory log after an ordinary question is not a fault.

### 6.3 Charlie write, restart, read drill

1. Send one synthesis request through the normal route. Expect memory log `operation` `create`, `outcome` `ok`, `scope` `agent/charlie`, and Charlie's `memory client` `decision` `ok`.
2. Restart everything (a manual deployment restarts all components, TESTED): `doctl apps create-deployment <app-id>`; wait for ACTIVE.
3. Send a second request. Expect `operation` `list`, `outcome` `ok` and `count` of at least 1 and a Charlie line with `decision` `ok`.

PASS: both lines present, no `denied` or `error`, no prompt, answer or token in any log. This is the managed-cluster equivalent of the local test that needs a Docker container.

**First window: PASS (WINDOW).** Charlie's read before synthesis returned `count` 0 (empty); the write after synthesis was `create` `ok`; a manual deployment restarted every component; the next request's read returned `count` 1 (the earlier record), followed by another `create` `ok`. Audit lines name caller `charlie` and scope `agent/charlie`; no content appeared in any log line. A restart of the app does not restart the managed database, so this shows the app path and rehydration, not database-level durability.

### 6.4 Failure drills (Charlie must still answer every time)

Do one at a time. Restore the correct value and redeploy between drills. Drill materials come from a separate generator run (`--out data/stage-memory-drill`), never from the live directory. **Only the memory-down and database-unreachable drills need no drill materials**; the wrong-certificate, expired-certificate and wrong-signing-key drills do.

**First window result: 2 of 5 PASS, 3 NOT RUN.** Database unreachable: PASS. Memory down: PASS. Wrong certificate, wrong signing key and expired certificate: **NOT RUN** (they need throwaway drill materials from a separate generator run; the generator cannot produce an already-expired certificate).

| Drill | How (founder) | PASS | FAIL |
|---|---|---|---|
| Memory down | set a wrong `MEMORY_DB_PASSWORD` on the `memory` component only, in the filled spec copy, redeploy | Charlie answers; one content-free `memory client` line, `reason` `http_503` (or `unreachable`, `timeout`); memory `/health` 503 (not observable from outside on Stage, see 6.2; the memory log's connect failure stands in) | Charlie errors or hangs past its time bound |
| Wrong certificate | paste `memory.crt.b64` from the drill directory into Charlie's `VIHARBOT_MEMORY_CA_PEM`, redeploy `synthesis` | answers; `reason` `tls_verification_failed`; no request sent | any request sent to an unverified server |
| Expired certificate | see note below | same as above | same |
| Wrong signing key | paste the drill `charlie-signing.key` into Charlie's `VIHARBOT_SIGNING_KEY`, keep the registry unchanged | answers; memory audit `denied` with `reason` `authentication_failed`; Charlie `reason` `denied` | a record written by the wrong key |
| Database unreachable | **placeholder swap, never an empty list** (see the method note below) | Charlie answers with `http_503` or `timeout`; the memory service logs `storage_unavailable` | Charlie fails |

**Database-unreachable method (WINDOW).** Removing the app's only trusted-source rule leaves the list **empty, which opens the cluster instead of blocking it**, so that is not a drill. Do it in this order: (1) add a placeholder `ip_addr` rule for a non-routable address (an address from the reserved documentation range was used), so the list is no longer empty; (2) remove the app rule with `remove --uuid`; (3) send a request; (4) restore by adding the app rule first, then removing the placeholder, so the list is never empty. Allow about 40 seconds for a rule change to take effect.

**Drill results (first window).**

- **Database unreachable: PASS.** The request still returned HTTP 200 with a synthesis. The memory service logged `storage_unavailable`; Charlie's client skipped both calls with reason `timeout` (about 1.5 seconds each, bounded).
- **Memory down (wrong `MEMORY_DB_PASSWORD` on the memory component only): PASS.** HTTP 200 with a synthesis; the memory service logged `memory store connect failed` (an `OperationalError`) and `storage_unavailable`; Charlie logged `http_503` for the read and for the write. After the correct spec was restored the read returned `count` 2: both earlier records survived both drills.
- **Wrong certificate, wrong signing key, expired certificate: NOT RUN.**
- An earlier, accidental observation: while the first cluster was deleted (4.6) the request path still returned HTTP 200 and Charlie skipped memory with `http_503`. This was not a planned drill and is not counted.

**Expired-certificate note.** `scripts/stage_memory_materials.py --days` accepts 1 to 3650 only, so it cannot produce a certificate whose `notAfter` is already past. The design's "past notAfter" drill therefore needs another way to make one (for example a one-off local script outside the repository by the founder) or is recorded as SKIPPED with the reason. Do not hand-edit the live materials for it. Both `memory` and Charlie must hold the same expired certificate for the failure to be a clock failure rather than a wrong-certificate failure.

After the drills, restore the live values, redeploy, and re-run 6.3 once to show memory works again.

**G4 is met** when 6.1 to 6.4 PASS and are in the window record. **First window: G4 is partially met**: health, write/restart/read, and two of five failure drills; three drills NOT RUN. Do not describe the build as verified beyond that. Any FAIL: stop and tell the founder; do not describe the build as verified.

## 7. Usage during the window

The window lasts the agreed days. The founder uses Stage normally; Charlie stores five metadata fields per synthesis, nothing else. Spot-check logs once a day: the `memory_stage_tls_certificate` line's `days_to_expiry`, no `error` outcomes, no content. Note any surprise in the window record. Do not add other agents to memory or store anything sensitive: backups last 7 days and there is no selective deletion, and secrets are visible to console users (design section 10). A window request that does not ask for a synthesis may not touch memory at all (6.2, live-team limitation). Do not extend the window silently; if it must run longer, update the cost line and tell the founder.

## 8. Records to keep after each window

Write this in a private note kept by the founder; do not paste logs wholesale and never a secret, ID of the founder's app, URL or address.

```text
Window record (non-secret)
Date range (UTC), who ran it: ______
Commit deployed (full SHA): ______    Merged PRs included: ______
Cluster: slug ______  PostgreSQL version ______  region ______  nodes 1  edition Standard
Price seen at creation (source): ______  CA differed from last window: yes / no / unknown (compare the CA SHA-256; first window: identical on a rebuilt cluster)
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

## 9. Built, and still not run (work items and checks)

Reconciled 2026-11-24 against `main` at commit `3bfc6c6` and the first window. Re-check each against `main` before the next window. When one changes, edit the matching step above.

**Built and merged:** launcher (PR #330), Charlie certificate pin and material generator (PR #329), migration runner schema skip (PR #328), `count` on the list audit line (PR #333), conformance fixture support (PR #334), probe retest kit (PR #331), the bootstrap entry (`MEMORY_STAGE_ROLE=bootstrap`, #354) with the `ALTER ROLE` fix (PR #361), and the Stage app spec (#356, with the per-component LiteLLM key slots and the web planner alias). Deployed to Stage once, on 2026-11-24, from `3bfc6c6`.

**Run in the first window (all WINDOW, one account):** the bootstrap on an empty cluster and again as a second run; the conformance suite on the window's first cluster (82 passed, 0 failed, 1 skipped); health; write, restart, read; two failure drills (memory down, database unreachable).

| Item | State | Affects |
|---|---|---|
| Wrong-certificate drill | **NOT RUN** (needs drill materials from a separate generator run) | G4 |
| Wrong-signing-key drill | **NOT RUN** (same) | G4 |
| Expired-certificate drill | **NOT RUN** (the generator cannot produce an already-expired certificate; needs another method or SKIPPED with the founder's acceptance) | G4 |
| Conformance on the rebuilt cluster | **NOT RUN**; the suite ran only on the window's first cluster | G2 evidence is for the first cluster only |
| Bootstrap-job-fails drill on Stage | **NOT RUN** (the failure behaviour is TESTED by the probe, not by this app's job) | acceptance row |
| The memory service's own `/health` response | **not observed** on Stage (no public route, TCP platform check, no `/health` log lines) | 6.2 |
| Deterministic memory use | **not built**: with the live team, memory runs only when the planner schedules synthesis | 6.2, 7 |
| Private-VPC trusted-source denial from an app attached to the VPC | **untested** | design |
| Creating an app with empty SECRET values | **untested** (the first window supplied values at creation) | 4.6 |
| Teardown (G5) | **not done**; only on the founder's explicit instruction at the end of the window | 10 |
| Invoice check of the window cost and the database slug price | **not done** | 3 |
| AGENTS.md sentence "Stage currently has the web service and six agents" | **stale** (Stage now also has the memory service and the bootstrap job); founder-reviewed path, not edited here; see `docs/stage-memory-acceptance.md` section 11 | none |

Places where the code differs from the design text (the code's name is used above):

| Design says | Code says |
|---|---|
| Client uses `ssl.create_default_context(cadata=...)` (5.3) | PR #329 (merged) builds a client context and loads only the supplied certificate; both `VIHARBOT_MEMORY_CA_FILE` and `VIHARBOT_MEMORY_CA_PEM` set and different gives reason `ca_conflict`; bad PEM gives `bad_ca_pem` |
| `MEMORY_DB_SSLMODE` must be set to `verify-full` (2.3) | with the launcher the mode is forced to `verify-full` when a CA is given; a non-loopback host without `MEMORY_DB_CA_PEM` is refused (PR #330, merged) |
| The launcher serves `serve` and `bootstrap` (item 6 "one image, role by variable") | `main` implements only `serve` (PR #330); the image runs the launcher whenever `MEMORY_STAGE_ROLE` is non-empty |
| Fresh materials per window (N2, N5) | the script's `--passwords-only` changes passwords only; a new certificate means `--force`, which also replaces the signing key and registry |

## 10. Teardown and nothing-left-billing checklist (gate G5)

Modeled on `deploy/digitalocean-probe/RUNBOOK.md` section 11, which the founder ran on 2026-11-12. Per decision N1, **the cluster is destroyed every window**. **Destroying the cluster destroys its backups, and memory starts empty at the next window.** Order: app (or memory pieces), then trusted-source rules, then the cluster, then local files.

**Option A: delete the whole `viharbot-stage` app** (the other seven components go too; use this only if the founder wants all of Stage off).

```text
RUN (founder)  doctl apps delete <app-id> --force               # TESTED
```

**Option B: keep the web and six agents; remove only the memory pieces.** Edit your local spec copy to remove the `memory` service, the `memory-bootstrap` job and the `memory-db` binding, remove Charlie's memory variables (above all `VIHARBOT_MEMORY_URL`, since memory is on whenever it is set), validate, apply with `doctl apps update <app-id> --spec data/stage-spec-filled.yaml`, and wait for ACTIVE. Then Charlie logs no memory call.

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
| Wrong cluster CA | `memory` runs, `/health` 503, Charlie reason `http_503`; platform shows the component healthy (TCP check) | 6.2 response, not the component state | re-save the CA from `doctl databases get-ca` for this cluster; compare its SHA-256 with the saved one (a rebuilt cluster on this account had an identical CA, so a new cluster does not always differ) |
| Trusted-source rule missing | job or memory cannot connect to the database; first deployment fails if any other trusted source exists (design 4); with an empty list nothing is blocked | `doctl databases firewalls list <cluster-id>` shows no `app:` rule | `append --rule app:<app-id>`, then `doctl apps create-deployment <app-id>` |
| Operator rule left behind | firewall list shows an `ip_addr` rule | the same list | `remove --uuid`; check section 10 |
| Conformance run killed (timeout) | `vbtest_` databases and roles remain on the cluster | `doctl databases db list` and `user list` | `db delete` and `user delete` for each `vbtest_` entry (4.4) |
| Conformance refuses to run | the empty-cluster guard names an unexpected database | `doctl databases db list <cluster-id>` shows the dedicated database | create the dedicated database after the run, or delete the empty one first (4.1) |
| Console edit reverted by an update | a variable is back to the committed value after `doctl apps update` | diff `doctl apps spec get` against the file before applying (4.6); secrets are masked in a diff | edit the file, not only the console |
| Variable mangled by the control panel | refusal lines like "is neither PEM text nor base64 of PEM" or "has no keys" | the refusal names the variable | paste the base64 `.b64` file; avoid editing a PEM in the browser |
| Port mismatch | component fails the TCP health check or never becomes healthy; `memory_stage_start` shows `port` 8000 | the `memory_stage_start` line | set `PORT` to `8443`; `internal_ports` and the health check must also say 8443 |
| Health 503 | 6.2 returns 503 or Charlie logs `http_503` | the memory log for a database error class (class only, no text); check CA, passwords, rule | fix whichever of CA, `MEMORY_DB_PASSWORD` or rule is wrong, redeploy `memory` |
| Charlie reason `denied` | memory audit `denied`, `authentication_failed` | key id: `VIHARBOT_SIGNING_KID` against the registry's key id (the script prints it); fingerprints | re-paste matching `charlie-signing.key` and `memory-registry.b64` from one generator run |
| Charlie reason `tls_verification_failed` | memory off, Charlie answers | compare certificate fingerprint printed by the script with the launcher's `fingerprint_sha256` | re-paste the same `memory.crt.b64` into both components; check expiry (`days_to_expiry`) |
| Database password mismatch after redeploy | `/health` 503 after `--passwords-only` | passwords for `memory` and the bootstrap come from the same file | set `MEMORY_DB_PASSWORD` equal to `MEMORY_DB_SERVICE_PASSWORD`, redeploy |
| `doctl` flag rejected | usage error | `doctl <command> --help` | use the control-panel fallback and record the flag in the window record |
| Anything content-like in a log | a prompt, answer or token appears | stop; do not copy it | tell the founder; this is a FAIL of G4 and a defect to report, not to work around |

If a step fails in a way this table does not cover, record the exact fixed text and the step, tear down if in doubt, and do not improvise around a security check (never switch a connection to `require` or disable hostname verification).
