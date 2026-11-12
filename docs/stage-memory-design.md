# Stage design: the governed memory service on DigitalOcean App Platform

**Status: design only. Nothing is provisioned, nothing is deployed, no Stage promotion is requested, and issue #40 stays open.** This document is a proposal for the founder to review. It is an ordinary design document under `docs/`, not an ADR and not a governance document. It changes no code and no app spec.

**Why this exists.** ADR-009 (Accepted, PostgreSQL everywhere) and issue #40 call for governed memory, and `docs/memory-service.md` says plainly that "There is no Stage or Production memory service, database or TLS design." The founder has since settled five Stage decisions (section 11) and a throwaway probe tested the platform assumptions (PR #326). This document turns those into one reviewable design, a work breakdown of small pull requests, and the evidence the founder should see before being asked "Do you want to promote this dev build to Stage?".

**Dependency on PR #326.** The probe results live in PR #326 (`docs/stage-memory-platform-verification.md`, section "Tested by throwaway probe"), which is not merged at the time of writing. This design cites it as "PR #326". If #326 merges first, read the same citations as that file on `main`. If it never merges, the observations below still stand as the author's reading of it, but should be re-checked against the file.

**Labels used throughout.** TESTED = observed by the founder's probe run on one account on 2026-11-12 (PR #326); that is observation, not DigitalOcean documentation. DOCUMENTED = a DigitalOcean page says it (as recorded in the verification document). LOCAL = observed by the author of this document on a throwaway PostgreSQL 17.11 container on a laptop; it is not DigitalOcean and not Stage. UNVERIFIED = nobody has checked it. UNTESTED = a fix or step designed here that has not been run anywhere. "Code" claims cite a file path; if a document and the code disagree, the code wins.

## 1. Purpose, scope and non-goals

**Purpose.** Describe how the existing memory service (`src/viharbot/memory_service/`), already running in local Compose, would run on Stage (DigitalOcean App Platform) in short "windows" of a few days, backed by a DigitalOcean Managed PostgreSQL cluster, with authenticated TLS on both hops (Charlie to memory, memory to database).

**What "memory on Stage" will mean, if this is ever built and promoted:**

| It will | It will not |
|---|---|
| Be used by Charlie (the synthesis service, `AGENT_KIND=synthesis`) only | Be used by Nova, Scout, Cipher, Pixel, Sage or the web service. They have no memory integration; the client logs `agent_not_integrated` for any other agent kind (`src/viharbot/charlie_memory.py`). |
| Store five metadata fields per synthesis and nothing else: `schema`, `sections_present`, `key_points_count`, `caveats_count`, `confidence` (`docs/memory-service.md`, `src/viharbot/charlie_memory.py::build_metadata`) | Store questions, answers, findings, free text, customer data or secrets. There is no content scanner (`docs/memory-service.md`), so this limit is a convention of what Charlie writes, not a control. |
| Use the agent-private tier only (`/memory/agent/charlie/records`); Charlie's policy has `session = []` (`src/viharbot/execution_config/memory_policy.toml`) | Offer any session, engagement or institutional tier. |
| Be best effort and fail open: if memory is down, slow or refused, Charlie still answers and logs one content-free line (`src/viharbot/memory_client.py`) | Make Charlie depend on memory. A broken memory service must never break a synthesis. |
| Be empty at the start of every window (the cluster is created and destroyed per window, and destroying it destroys its backups, DOCUMENTED, PR #326 row 3f) | Persist anything between windows, or serve as a durable record. |
| Be active whenever `VIHARBOT_MEMORY_URL` is set (no feature flag, `src/viharbot/charlie_memory.py::load_client`) | Have an on/off switch. Turning it off means removing the URL. |

**Non-goals.** No Production design (AWS and GCP stay separate, explicit choices). No other agent's memory. No high availability. No selective backup deletion. No change to ADR-009, ADR-006, AGENTS.md, `.github/` or `docs/governance/`. No implementation in this pull request, and no request to deploy anything.

**What this document does not claim.** It does not claim Stage is ready, that memory works on Stage, or that any gate is complete. Issue #40 stays open; this pull request refers to it only as "Refs #40".

## 2. Architecture on Stage

### 2.1 Components

| Component | Kind | Status | Public route | Talks to |
|---|---|---|---|---|
| `web` | service | existing, unchanged | yes (Basic Auth except `/health`) | the six agents (plain HTTP on internal port 8000) |
| `research`, `security`, `fact-check`, `source-quality`, `privacy-review` | services | existing, unchanged, no memory | no (`internal_ports: 8000`) | each other and LiteLLM as today |
| `synthesis` (Charlie) | service | existing; gains memory variables only | no | LiteLLM; the new `memory` service over HTTPS |
| `memory` | service | **new** | **none**: `internal_ports` only, no `http_port`, no ingress rule | the managed database over TLS |
| `memory-bootstrap` | `PRE_DEPLOY` job | **new** | none (jobs cannot receive requests, DOCUMENTED) | the managed database as admin, then as migrator |
| `memory-db` | managed database binding, trusted source | **new** (and the cluster itself, created by the founder outside the spec) | none | `memory` and `memory-bootstrap` only |

The current spec is `deploy/digitalocean/app.yaml`: web plus six agents, `MEMORY_DIR=/tmp/viharbot-memory`, `deploy_on_push: false` on every GitHub-backed component. This design adds components to it in a later, separately approved pull request (section 6, item 11). It does not edit that file.

### 2.2 Who talks to whom

| From | To | Over | Authentication | Verified by |
|---|---|---|---|---|
| browser | `web` | public HTTPS (platform ingress) | Basic Auth | existing |
| `web` | agents | plain HTTP, private network | none on Stage today | existing (`docs/deployment-strategy.md`: Stage verifies and signs nothing) |
| Charlie | `memory` | **HTTPS on internal port 8443, served by the memory service itself** | ADR-006 A2 signed assertion, subject `charlie`, audience `memory` | server certificate pinned in Charlie; assertion verified against the memory registry |
| `memory` | database | TLS 1.3, `sslmode=verify-full` | role `memory_service` and password | cluster CA pasted as a variable |
| `memory-bootstrap` | database | same | admin credentials from the binding, then `memory_migrator` | same CA |
| anything else | database | refused by the trusted-source rule (denial UNTESTED, see 7.3) | | |

Charlie to memory is the only new application hop. The platform's own treatment of internal traffic is not relied on: whether App Platform encrypts it is not documented and the application cannot observe it (PR #326 row 1a). Both new hops therefore do their own TLS with verification on.

### 2.3 Environment variables per component

Names marked "existing" are read by the code today; names marked **new** do not exist yet and are what the work breakdown adds. Type is the App Platform variable type (`SECRET` = encrypted, `GENERAL` = plain). All values are placeholders; no real value belongs in the repository.

**`synthesis` (Charlie), additions only**

| Variable | Type | Value | Code |
|---|---|---|---|
| `VIHARBOT_MEMORY_URL` | GENERAL | `https://memory:8443` | existing, `src/viharbot/memory_client.py` (`URL_ENV`) |
| `VIHARBOT_MEMORY_CA_PEM` | SECRET | base64 of the memory certificate PEM | **new** (existing: `VIHARBOT_MEMORY_CA_FILE`, a path) |
| `VIHARBOT_OUTBOUND_AUTH` | GENERAL | `on` | existing, `src/viharbot/outbound_auth.py` |
| `VIHARBOT_SIGNING_KID` | GENERAL | `charlie-stage-1` | existing, `outbound_auth.py` |
| `VIHARBOT_SERVICE_SUBJECT` | GENERAL | `charlie` (the client refuses any other value, `memory_client.py`) | existing |
| `VIHARBOT_SIGNING_KEY` | SECRET | base64url Ed25519 seed | existing: `service_auth.load_signing_key` already reads this variable when no key file is set, so no code change is needed for Charlie's key |
| `VIHARBOT_MEMORY_TIMEOUT_SECONDS` | GENERAL | optional, default 1.5, at most 10 | existing |

**`memory` (new component), environment**

| Variable | Type | Value | Code |
|---|---|---|---|
| `PORT` | GENERAL | `8443` | existing, `Dockerfile.memory` (`ENV PORT`) |
| `MEMORY_TLS_CERT_PEM`, `MEMORY_TLS_KEY_PEM` | SECRET | base64 PEM | **new** (existing: `MEMORY_TLS_CERT_FILE`, `MEMORY_TLS_KEY_FILE`, paths) |
| `VIHARBOT_KEY_REGISTRY_B64` | SECRET | base64 of the registry TOML (public keys) | **new** (existing: `VIHARBOT_KEY_REGISTRY`, a path, `src/viharbot/memory_service/app.py`) |
| `MEMORY_DB_HOST`, `MEMORY_DB_PORT` | GENERAL | `${memory-db.HOSTNAME}`, `${memory-db.PORT}` | existing names, `app.py::conninfo_from_environment`; the bindable names are TESTED for `HOSTNAME` and `PORT` (PR #326) |
| `MEMORY_DB_NAME` | GENERAL | the dedicated database name | existing |
| `MEMORY_DB_USER` | GENERAL | `memory_service` | existing |
| `MEMORY_DB_PASSWORD` | SECRET | service role password | existing |
| `MEMORY_DB_SSLMODE` | GENERAL | `verify-full` (the code default is `prefer`, so it must be set) | existing |
| `MEMORY_DB_CA_PEM` | SECRET | base64 of the cluster CA PEM | **new** |

**`memory-bootstrap` (new `PRE_DEPLOY` job), environment**

| Variable | Type | Value | Code |
|---|---|---|---|
| `MEMORY_STAGE_ROLE` | GENERAL | `bootstrap` (one image, one command, role chosen by variable, like `PROBE_ROLE`) | **new** |
| `MEMORY_ADMIN_DB_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_NAME` | GENERAL / SECRET | `${memory-db.HOSTNAME}`, `.PORT`, `.USERNAME`, `.PASSWORD`, `.DATABASE` | **new** names, read through the existing `conninfo_from_environment(prefix=...)`. `DATABASE_URL`, `HOSTNAME`, `PORT`, `USERNAME` are TESTED; `PASSWORD` and `DATABASE` are DOCUMENTED only (PR #326 row 4d). |
| `MEMORY_ADMIN_DB_SSLMODE` | GENERAL | `verify-full` | **new** |
| `MEMORY_DB_MIGRATOR_PASSWORD`, `MEMORY_DB_SERVICE_PASSWORD` | SECRET | the two role passwords | existing names in `deploy/postgres/init/10-memory-roles.sh`; reused |
| `MEMORY_MIGRATE_DB_NAME`, `_USER` (`memory_migrator`), `_HOST`, `_PORT`, `_SSLMODE` | GENERAL | same host and port as above | existing names, `src/viharbot/memory_service/migrate.py`; the bootstrap maps `MEMORY_DB_MIGRATOR_PASSWORD` onto the migrate prefix so the password is pasted once |
| `MEMORY_DB_CA_PEM` | SECRET | same cluster CA | **new** |

`web` and the five other agents get no memory variables. A repository test asserts the web component carries none (`tests/test_memory_deployment_static.py`).

### 2.4 Proposed app spec fragment (new components only)

This is a **proposal for discussion, not a file to apply.** It omits secret values, the existing components and the top-level settings. `<cluster-name>` and `<database-name>` are placeholders. Instance sizes are an assumption (section 9). Whether `run_command` is honoured for a Dockerfile component is UNVERIFIED, so the design avoids it and selects the role with `MEMORY_STAGE_ROLE`, the pattern the probe used (`deploy/digitalocean-probe/probe_entry.py`).

```yaml
databases:
  - name: memory-db
    engine: PG
    production: true
    cluster_name: <cluster-name>
    db_name: <database-name>
    db_user: doadmin

services:
  - name: memory                      # no http_port and no ingress rule: no public route
    github: { repo: vchokshi/viharbot, branch: main, deploy_on_push: false }
    dockerfile_path: Dockerfile.memory
    source_dir: /
    instance_size_slug: apps-s-1vcpu-0.5gb
    instance_count: 1                  # one instance: the jti replay cache is per process
    internal_ports: [8443]
    health_check: { port: 8443 }       # TCP check; an HTTPS service cannot answer a plain HTTP check
    envs:
      - { key: MEMORY_STAGE_ROLE, value: serve, scope: RUN_TIME, type: GENERAL }
      - { key: PORT, value: "8443", scope: RUN_TIME, type: GENERAL }
      - { key: MEMORY_TLS_CERT_PEM, value: "", scope: RUN_TIME, type: SECRET }   # paste, never commit
      - { key: MEMORY_DB_HOST, value: "${memory-db.HOSTNAME}", scope: RUN_TIME, type: GENERAL }
      # ...remaining MEMORY_DB_* and registry variables as in section 2.3

jobs:
  - name: memory-bootstrap
    kind: PRE_DEPLOY
    github: { repo: vchokshi/viharbot, branch: main }
    dockerfile_path: Dockerfile.memory
    source_dir: /
    envs:
      - { key: MEMORY_STAGE_ROLE, value: bootstrap, scope: RUN_TIME, type: GENERAL }
      - { key: MEMORY_ADMIN_DB_PASSWORD, value: "${memory-db.PASSWORD}", scope: RUN_TIME, type: SECRET }
      # ...remaining variables as in section 2.3
```

The trusted-source rule is not in the spec: it is a property of the cluster, added by the founder with `doctl databases firewalls append <cluster-id> --rule app:<app-id>` (TESTED: the permitted app connected afterwards; denial of others UNTESTED).

## 3. Secrets and material inventory

**Console visibility, stated plainly.** Encrypted App Platform variables hide values in logs and in the spec, but DigitalOcean's documentation says they "can be visible to other team members who can access the application's console" (DOCUMENTED, PR #326 row 7a). Treat every row below as readable by anyone with console access to the Stage app or the DigitalOcean team. This is the same lab-grade risk the founder accepted for signing keys in ADR-006, and it is why decision 4 (7-day backups) and the metadata-only limit matter. Revisit before any agent stores anything sensitive.

| Item | Component | Variable | Type | Generated by | Size (est.) | Rotation | Who can see it | If it leaks | If it expires or is wrong |
|---|---|---|---|---|---|---|---|---|---|
| Memory server certificate (self-signed, public) | `memory` | `MEMORY_TLS_CERT_PEM` | SECRET | Stage-material generator, local only (item 6) | ~0.7 KB base64 | yearly, or on key loss | console users | nothing by itself (public) | see 5.5 |
| Memory server private key | `memory` | `MEMORY_TLS_KEY_PEM` | SECRET | same | ~0.3 KB base64 | with the certificate | console users | an attacker inside the app's network could impersonate `memory` to Charlie, collect Charlie's short-lived signed assertions (30 s, bound to method, target and body) and serve poisoned prior-memory text (wrapped as untrusted data). Replace certificate and key everywhere. | server cannot load the pair: it refuses to start |
| Trusted certificate (the same certificate, Charlie's only trust anchor) | `synthesis` | `VIHARBOT_MEMORY_CA_PEM` | SECRET | same output file | ~0.7 KB | with the certificate | console users | nothing (public) | Charlie loses memory, still answers (5.5) |
| Charlie's signing key (Ed25519 seed) | `synthesis` | `VIHARBOT_SIGNING_KEY` | SECRET | generator | 43 chars | 30 to 90 days with overlap per ADR-006 (`docs/service-authentication.md`); in a lab, per window is cheap | console users | an attacker with network access to `memory` can read and write Charlie's agent-private scope (five metadata fields) until the `kid` is removed from the registry | `denied` at memory; Charlie answers without memory |
| Memory key registry (public keys, TOML) | `memory` | `VIHARBOT_KEY_REGISTRY_B64` | SECRET (public data, kept uniform) | generator | ~0.4 KB | with Charlie's key; two entries for one `sub` give an overlap (`docs/service-authentication.md`) | console users | nothing (public keys) | service refuses to start with no keys (`service_auth.KeyRegistry.load_initial`) |
| Database role password, service | `memory`, `memory-bootstrap` | `MEMORY_DB_PASSWORD`, `MEMORY_DB_SERVICE_PASSWORD` | SECRET | generator, 16+ characters (the Compose script enforces 16) | 32 chars | per window (new cluster each window) | console users | read, insert, update, delete on the five record tables only; usable only from a trusted source and over TLS (denial UNTESTED) | memory cannot connect: `/health` 503, Charlie fails open |
| Database role password, migrator | `memory-bootstrap` only | `MEMORY_DB_MIGRATOR_PASSWORD` | SECRET | generator | 32 chars | per window | console users | DDL on the `memory` schema | job fails, deploy blocked, old version keeps serving |
| Cluster CA certificate | `memory`, `memory-bootstrap` | `MEMORY_DB_CA_PEM` | SECRET | founder copies it from the control panel or `doctl databases get-ca` (TESTED); no platform variable provides it | ~2 KB base64 (TESTED at 2,056 characters) | **every new cluster** (UNVERIFIED that a new cluster has a new CA; assume it does) | console users | nothing (public) | `verify-full` fails: bootstrap fails, deploy blocked |
| Database admin credentials | `memory-bootstrap` | from the binding (`${memory-db.*}`) | SECRET | platform | n/a | the cluster's lifetime | console users, and anyone who can read bindings | full control of the cluster: reset everything | n/a |

The generator writes only to a gitignored directory (`data/` is already gitignored, `.gitignore`), prints fingerprints and expiry but never a key or password, and the founder pastes values into a local filled copy of the spec or the console, exactly as the probe runbook did (`deploy/digitalocean-probe/RUNBOOK.md`). **Encrypting on first submission and creating an app with empty `SECRET` values were not tested** (PR #326 row 7a), and variables above about 2 KB are untested (row 7b); the cluster CA is the one value near that size.

## 4. Database bootstrap and migrations on a managed cluster

### 4.1 Sequence the pre-deploy job performs

The founder creates the cluster and the dedicated database (as the probe runbook step 2 did); the database is owned by `doadmin`. Then, on every deployment, `memory-bootstrap` does:

1. **Connect as admin** with `sslmode=verify-full` and the cluster CA from `MEMORY_DB_CA_PEM`, using the binding's host name (the CA is written to a file because libpq needs a path, and it stays for the process lifetime; it is public).
2. **Create or alter the two roles** with the supplied passwords: if the role exists, `ALTER ROLE ... LOGIN PASSWORD`, else `CREATE ROLE ... LOGIN PASSWORD`. Neither role gets `SUPERUSER`, `CREATEDB` or `CREATEROLE`. Pre-hash the password client-side with SCRAM (libpq `encrypt_password`) so the plaintext is not in the statement text the server may log (UNTESTED; whether the cluster logs DDL text is UNVERIFIED).
3. **Let admin act as the migrator role**: `GRANT memory_migrator TO CURRENT_USER WITH SET TRUE`. LOCAL: on plain PostgreSQL 17.11 a non-superuser admin that created the role cannot `ALTER SCHEMA ... OWNER TO` it, nor `ALTER DATABASE ... OWNER`, without this ("must be able to SET ROLE"). TESTED on DigitalOcean: `ALTER SCHEMA ... OWNER` worked without it (PR #326 row 5b), so DigitalOcean's `doadmin` differs from my local stand-in. The statement is harmless if redundant. UNTESTED on DigitalOcean.
4. **Database privileges**: `REVOKE ALL ON DATABASE ... FROM PUBLIC`, then `GRANT CONNECT` to both roles. **The migrator CONNECT grant is the fix for the probe's failure** ("permission denied for database": the platform-created database is owned by `doadmin` and public CONNECT had been revoked). LOCAL: reproduced the same error on PostgreSQL 17.11 and confirmed the grant removes it. **UNTESTED on DigitalOcean.** The alternative, `ALTER DATABASE ... OWNER TO memory_migrator`, worked LOCAL once step 3 had run; also UNTESTED on DigitalOcean.
5. **Schema**: `CREATE SCHEMA IF NOT EXISTS memory AUTHORIZATION memory_migrator`, then `ALTER SCHEMA memory OWNER TO memory_migrator` (a no-op once owned). TESTED on DigitalOcean: `CREATE SCHEMA` and `ALTER SCHEMA ... OWNER` worked.
6. **Migrate as `memory_migrator`** with the existing runner (`src/viharbot/memory_service/migrate.py`): numbered plain SQL, one transaction per file, advisory lock, checksum refusal of edited history.

**A finding that makes this harder than the founder's decisions assume.** `migrate.py` runs `CREATE SCHEMA IF NOT EXISTS memory` first. LOCAL: as a migrator that owns the schema but lacks `CREATE` on the database, that statement fails with "permission denied for database" even though the schema exists; it succeeded once the migrator was granted `CREATE` on the database. Compose never meets this because the migrator owns the database there (`deploy/postgres/init/10-memory-roles.sh`). Two ways out: grant `CREATE ON DATABASE` to the migrator (it could then create other schemas), or change `migrate.py` to skip the statement when the schema already exists. This design recommends the code change (decision N6) and lists it as work item 5.

### 4.2 Why every step must be idempotent

The job runs before every deploy and, after a failed deploy, an automated rollback deployment appears to run it again (TESTED, inconclusive: one extra run between the failed deployment and the next; a platform retry cannot be ruled out, PR #326 row 6c). A job that is not safe to repeat would fail the rollback too. The migration runner is already safe to repeat (checksum and advisory lock, `migrate.py`; migration `0002` uses `ADD COLUMN IF NOT EXISTS`); the role and grant steps are made repeatable by the create-or-alter and by `GRANT`/`REVOKE` being naturally repeatable. TESTED: a second run of the probe's role bootstrap exited 0 and altered rather than re-created. The job must also hold nothing that a half-finished run could leave inconsistent: each step is independently correct.

### 4.3 Compared with `deploy/postgres/init/10-memory-roles.sh`, statement by statement

| Compose script | Stage bootstrap | Mark |
|---|---|---|
| `CREATE ROLE memory_migrator LOGIN PASSWORD` | create-or-alter, same attributes, password supplied by variable, hashed client-side | **changed** (idempotent) |
| `CREATE ROLE memory_service LOGIN PASSWORD` | same | **changed** (idempotent) |
| `CREATE DATABASE ... OWNER memory_migrator` | not run: the database already exists, owned by `doadmin`; the job checks it exists | **changed** |
| `REVOKE ALL ON DATABASE ... FROM PUBLIC` | same | **same** (TESTED, probe) |
| `GRANT CONNECT ON DATABASE ... TO memory_service` | same | **same** |
| (implicit: the migrator owns the database, so it can connect) | `GRANT CONNECT ... TO memory_migrator` | **new, UNTESTED on DigitalOcean** |
| (not needed) | `GRANT memory_migrator TO CURRENT_USER WITH SET TRUE` | **new, UNTESTED on DigitalOcean** |
| (the migrator creates the schema itself as database owner) | `CREATE SCHEMA ... AUTHORIZATION memory_migrator` and `ALTER SCHEMA ... OWNER` | **new** (TESTED without `AUTHORIZATION`) |
| (not needed) | `migrate.py` skips `CREATE SCHEMA` when the schema exists, or `GRANT CREATE ON DATABASE` | **new** (LOCAL finding) |
| (table privileges) | still granted by migration `0001` explicitly (`GRANT ... TO memory_service`, `src/viharbot/memory_service/migrations/0001_memory_schema.sql`) | **same** |
| (n/a) | `ALTER DEFAULT PRIVILEGES` | **not added**: the probe used it, but the product's migrations grant each table explicitly, so it would only widen the service role. A future table needs its own `GRANT` in its migration, as today. |
| (n/a) | optional `REVOKE CREATE ON SCHEMA public FROM PUBLIC` | **optional, UNTESTED**: LOCAL, the service role was already refused `CREATE` in `public` (PostgreSQL 15 and later default) |

### 4.4 How the service connects

`memory` uses role `memory_service`, `sslmode=verify-full`, the cluster CA from `MEMORY_DB_CA_PEM` and the host name from the binding (`${memory-db.HOSTNAME}`). TESTED: `verify-full` and `verify-ca` with the pasted CA worked, `verify-full` against system roots failed, the injected `DATABASE_URL` carries only `sslmode=require` (which does not authenticate the server) and is therefore not used by the service (PR #326 rows 3k, 4e). The injected host name has no `private-` prefix yet reached a private address (row 3l); that is observation, and a risk (section 10). The service opens one connection per operation (`src/viharbot/memory_service/postgres.py`), and the 1 GiB plan allows 22 backend connections (DOCUMENTED, row 3i); with one Charlie instance that is ample.

Code today cannot do any of this: `conninfo_from_environment` has no CA setting and defaults to `prefer` (`app.py`). Work item 4 adds it, and refuses to start on a non-loopback host unless the mode is `verify-full`.

## 5. TLS and trust design for Charlie to memory

### 5.1 The certificate

One self-signed certificate, EC P-256, valid 365 days, key usage limited to a TLS server, `BasicConstraints CA:FALSE`, one subject alternative name: **`memory`**, the plain component name. TESTED: the plain name resolves and verifies against a pinned self-signed certificate; the long `<component>.app-<app-id>.svc.cluster.local` name resolves but embeds the app ID (it changes when the app is recreated, which this design does every window) and was not in the certificate (PR #326 rows 2d, 1d). No other names: `localhost` is not needed because the platform health check is TCP, and every extra name widens what the certificate is trusted for. This follows the probe generator (`deploy/digitalocean-probe/gen_probe_cert.py`).

Python's TLS library accepts a pinned self-signed leaf as the only trust anchor on the Python 3.12 images this repository builds (TESTED, the probe's client). UNVERIFIED on Python 3.13 and later, which tightened X.509 strictness; recheck when the base image changes.

### 5.2 Server side: certificate and key from variables

`memory` decodes `MEMORY_TLS_CERT_PEM` and `MEMORY_TLS_KEY_PEM` (base64, which the probe proved survives the control panel; raw PEM may have its newlines flattened), writes them into a private 0700 temporary directory as 0600 files, loads them, and deletes them at once. The probe does this for its Python HTTP server (`probe_common.TempSecretFiles`, `probe_service.build_server_context`, minimum TLS 1.2). The product server is uvicorn, which takes file paths, so the new launcher builds the uvicorn config (which loads the certificate), removes the files, then runs the server. That ordering is how the launcher intends to work and is UNVERIFIED until the offline test and the local rehearsal show uvicorn does not reread the files. `Dockerfile.memory` today runs a shell `CMD` that checks `MEMORY_TLS_CERT_FILE` and passes `--ssl-certfile`; the Compose path must keep working, so the launcher is a new entry selected by `MEMORY_STAGE_ROLE` and the file-based path stays for Compose.

### 5.3 Client side: trusted certificate from a variable

Charlie builds its TLS context from `VIHARBOT_MEMORY_CA_PEM` with `ssl.create_default_context(cadata=...)`, which loads only that certificate (system roots are not added when `cadata` is given) with hostname verification left on. Today the client only accepts a file path (`CA_FILE_ENV`, `memory_client.client_from_environment`); the new variable is read when the file variable is empty, and a bad value yields the existing `bad_ca_file` refusal, which keeps memory off without stopping Charlie. Hostname verification is never disabled.

### 5.4 Prior art: reuse versus re-implement

| Probe piece | Reuse as is | Re-implement in product code |
|---|---|---|
| `gen_probe_cert.py` (certificate shape, one name, one year, refuses to write outside `data/`) | the design | a Stage-material generator under `scripts/` (the probe kit is throwaway and is deleted or ignored) |
| `probe_common.decode_pem_variable` (raw, `\n`, flattened, or base64) | the idea | a small shared helper, tested offline |
| `probe_common.TempSecretFiles` (0700 directory, 0600 files, removed on exit) | the idea | in `src/viharbot/memory_service/` |
| `probe_service.build_server_context` | the idea | around uvicorn, not `http.server` |
| `probe_job` (role bootstrap, scratch schema) | the sequence | the real bootstrap with the real role names and the extra grants in 4.1 |
| `probe_entry.py` (role by variable) | the pattern | `MEMORY_STAGE_ROLE` entry |

Nothing in `deploy/digitalocean-probe/` is imported by product code.

### 5.5 Renewal and what happens when the certificate expires

Renewal is a yearly task for the founder: run the generator, paste the new certificate and key into `memory` and the new certificate into Charlie's `VIHARBOT_MEMORY_CA_PEM`, deploy both. During the one deploy the two sides may briefly disagree; both fail open. Because the whole cluster is rebuilt each window, a fresh certificate per window is also practical; the decision is N5.

If the certificate expires (or the two copies differ), the memory service keeps serving it (it does not check its own expiry), Charlie's handshake fails with certificate verification, the client records the content-free reason `tls_verification_failed` (`src/viharbot/memory_client.py`), and Charlie answers without memory. Nothing else breaks. To make expiry visible before it happens, the launcher should log the days remaining at start (a number, content-free), proposed in item 1.

## 6. Code and configuration changes required

No item below touches a founder-reviewed path (`docs/adr/`, `docs/governance/`, `.github/`, `AGENTS.md`, `CONTRIBUTING.md`, `CONTRIBUTORS.md`, the CLA files), with one note under item 11. Sizes are rough: small is under a day, medium a day or two, large more.

| # | Change | Files touched | Size | Tests needed | Depends on |
|---|---|---|---|---|---|
| 1 | Memory service accepts TLS certificate and key from encrypted variables; Stage launcher selected by `MEMORY_STAGE_ROLE=serve`; logs days to expiry | new `src/viharbot/memory_service/stage_entry.py`, `Dockerfile.memory` | medium | offline: bad base64, missing key, mismatched pair, files removed after load, Compose file mode unchanged | none |
| 2 | Memory service accepts its key registry from a variable (writes it to a private file and uses the existing `KeyRegistry`) | `stage_entry.py` | small | offline: valid, malformed, empty registry (service must refuse to start) | 1 |
| 3 | Client accepts the trusted certificate from a variable | `src/viharbot/memory_client.py` | small | offline: only that certificate is trusted, wrong certificate fails closed, hostname mismatch fails, file variable still works | none |
| 4 | Database connection with `verify-full` and the CA from a variable; refuse non-loopback without `verify-full` | `src/viharbot/memory_service/app.py`, `stage_entry.py` | small | offline: conninfo contains `sslrootcert` and `verify-full`; refusal case | 1 |
| 5 | Migration runner skips `CREATE SCHEMA` when it exists | `src/viharbot/memory_service/migrate.py` | small | database test (local): owner without database `CREATE` migrates cleanly; Compose path unchanged | none |
| 6 | Stage bootstrap-and-migrate entry (`MEMORY_STAGE_ROLE=bootstrap`) implementing 4.1 | new `src/viharbot/memory_service/stage_bootstrap.py`, `stage_entry.py` | medium | offline with a fake gateway (as `probe_job` is tested); database test (local) against a non-superuser admin stand-in; idempotent twice in a row | 4, 5 |
| 7 | Stage-material generator: certificate, signing key, registry, role passwords, outputs only to a gitignored directory, prints fingerprints and expiry only | new `scripts/stage_memory_materials.py` | small | offline: refuses an output path outside `data/`, file modes 0600, no secret in stdout, registry parses with `parse_registry` | none |
| 8 | Optional: a `count` number in the list audit line so a write, restart, read drill is observable without reading content | `src/viharbot/memory_service/audit.py`, `app.py` | small | offline: the audit scan test still finds no content | none |
| 9 | Conformance run against a managed cluster: parameterize the test fixture (it creates roles named `memory_migrator` and `memory_service` and `ALTER`s their passwords, which would break a live service), skip the container-restart test, accept a `verify-full` admin DSN | `tests/memory_helpers.py`, `docs/memory-service.md` | medium | the suite itself, local first | 6 |
| 10 | Stage runbook and the first window's checklists | new `docs/stage-memory-runbook.md` | small | none (documentation) | 1 to 9 |
| 11 | App spec change for Stage: `memory`, `memory-bootstrap`, `memory-db`, Charlie's variables; update `test_the_digitalocean_spec_is_untouched` deliberately | `deploy/digitalocean/app.yaml`, `tests/test_memory_deployment_static.py` | medium | offline: static spec tests (no web memory variables, no secret values, `deploy_on_push` false, no public route for `memory`) | all; **a separate pull request that needs the founder's explicit promotion approval for the build** |
| 12 | Documentation updates: Stage statement in `docs/memory-service.md`, `docs/deployment-strategy.md`, `docs/development-status.md` | those three files | small | none | 11 |

**Note on item 11 and a protected path.** `AGENTS.md` says "Stage currently has the web service and six agents". That sentence would become stale once item 11 is deployed. `AGENTS.md` is a founder-reviewed path, so this design does not edit it; the founder decides when and how to update it.

**Conformance run (item 9).** ADR-009 requires the contract suite against the Stage database before adoption (`docs/memory-service.md`, "CI: what can and cannot run"). Proposal: run it from the operator's machine against the window's **fresh cluster before the app is created**, with a temporary trusted-source rule for the operator's address (`doctl databases firewalls append <cluster-id> --rule ip_addr:<operator-address>`), removed in the same session and checked in teardown. The admin DSN uses `verify-full` and the cluster CA. In scope: every test in `tests/test_memory_postgres_contract.py` (55 test functions at the time of writing, including the in-process write, restart, retrieve test) except `test_records_survive_a_database_container_restart`, which needs a local Docker container and cannot run against a managed cluster; the managed equivalent is the Stage drill in 7.5. Out of scope: `tests/test_memory_service_unit.py` and the static tests (no database; they run in CI). **Hard rule:** never run it against a cluster whose roles belong to a live service, because the fixture `ALTER`s the two roles' passwords (`tests/memory_helpers.py::provision_database`) and drops databases with `FORCE`. The fixture also creates its test database `OWNER memory_migrator`, which on a non-superuser admin may need the `WITH SET TRUE` grant from 4.1 (UNTESTED on DigitalOcean).

**Proposed order of small, independently reviewable pull requests**

1. Items 3 and 7 (client trust variable; generator). Independent, smallest, reviewable alone.
2. Items 1 and 2 (memory launcher: certificate, key, registry from variables).
3. Item 5 (migration runner) and item 4 (verify-full), together or separately.
4. Item 6 (bootstrap and migrate), after the retest in 7.3 has answered the CONNECT question.
5. Item 9 (conformance support) and item 8.
6. Item 10 (runbook), then a local rehearsal (7.2).
7. Item 11 (app spec) as the last code PR, only when the founder asks for it; deployment needs the promotion answer for that build.
8. Item 12 (documentation) with or after item 11.

## 7. Verification and acceptance

Before the founder is asked "Do you want to promote this dev build to Stage?", each row below needs evidence in the promotion summary. A row passes only on its stated PASS condition; anything else is FAIL, and the build is not offered.

### 7.1 Offline tests

PASS: the new offline tests for each variable mode (items 1 to 8) pass in CI, including malformed, empty, mismatched and absent inputs, with the existing suite unchanged. FAIL: any skipped or failing test; a test that needs PostgreSQL in CI (decided: CI does not run PostgreSQL, `docs/memory-service.md`).

### 7.2 Local rehearsal

Run the new launcher and bootstrap in Compose (or with the probe pattern) against the local PostgreSQL container with a **non-superuser admin stand-in**, a self-signed pinned certificate, a registry and signing key from variables, `verify-full`, and the bootstrap run twice. PASS: second run exits 0 with no change; Charlie's client writes, the service restarts, a read returns the record; no secret in any log line. FAIL: any plaintext fallback, any disabled verification, any statement that needs superuser.

### 7.3 Probe retests (one throwaway cluster, founder-run)

| Retest | PASS | FAIL |
|---|---|---|
| Untrusted-client denial: with the app as the only trusted source, a connection from an untrusted client (operator laptop, rule removed) is refused | refused | accepted: the trusted-source rule cannot be relied on, and the design says so instead |
| Migrator CONNECT grant (4.1 step 4), and the `WITH SET TRUE` need (step 3) | migrator logs in; role and schema steps succeed as `doadmin` with no superuser | still denied: switch to the owner-change alternative and retest |
| `CREATE SCHEMA IF NOT EXISTS` as the migrator without database `CREATE` | fails as in the local finding, confirming item 5 is needed | succeeds on DigitalOcean: item 5 stays as defence in depth |
| Retry versus rollback: a deliberately failing job, count the runs | the number of runs per failed deployment is known and every step is safe at that count | still inconclusive: keep "migrations must be idempotent" as the rule |
| Empty `SECRET` values at app creation; a variable larger than the cluster CA | either result is recorded and the runbook follows it | n/a (information) |
| Health check on `memory`: TCP works; an HTTPS check does not | recorded | n/a (information) |
| `verify-full` with a **new** cluster's CA | the CA differs per cluster, or not; recorded and the runbook says which | n/a (information) |

### 7.4 Conformance suite on a throwaway cluster

PASS: all in-scope tests (section 6, item 9) pass against the managed cluster with the same plan and PostgreSQL major version as the window will use, and the record of the run (date, commit, cluster plan, result counts, no connection details) is in the promotion summary. FAIL: any failure, any skip other than the container-restart test.

### 7.5 Charlie drill: write, restart, read

On the window's app: send a synthesis request, confirm the memory audit line `create ... ok` (identifiers only), restart the app (a manual deployment restarts everything, PR #326), send a second request, confirm a `list ... ok` line, and (with item 8) a count of at least one. PASS: both lines present, no content in any log. FAIL: no `create`, no `list`, a `denied` or `error`, or any prompt, answer or token in a log.

### 7.6 Failure drills (Charlie must still answer every time)

| Drill | How | PASS | FAIL |
|---|---|---|---|
| Memory down | make `memory` unavailable (for example redeploy it with a wrong database password, or stop it) | Charlie answers; one content-free line, reason `unreachable`, `timeout` or `http_503` | Charlie errors or hangs past its time bound |
| Wrong certificate | paste a different certificate into Charlie's variable | answers; reason `tls_verification_failed`; no request sent | any request sent to an unverified server |
| Expired certificate | a certificate generated with a past `notAfter` in the throwaway window only | same as above | same |
| Wrong signing key | paste a different seed into Charlie | answers; memory audit shows `authentication_failed`; Charlie reason `denied` | a record written by the wrong key |
| Database unreachable | remove the trusted-source rule | memory `/health` 503; Charlie answers with reason `http_503` or `timeout` | Charlie fails |

## 8. Stage window runbook outline

The founder runs every DigitalOcean action. This repository's agents never run `doctl`, never see credentials, and never contact DigitalOcean. Gates are explicit: nothing proceeds past a gate without the founder's words.

**Gates.** G0: this design merged (a founder-reviewed decision, not an implementation). G1: the founder approves each code pull request in section 6 by review and merge. G2: the founder approves a retest or conformance window (a throwaway cluster, a few hours, section 9 cost). G3: the founder answers the promotion question for one specific build. G4: after deploy, the founder reads the verification result. G5: teardown.

| Step | Who | What |
|---|---|---|
| 1 Prepare | founder | run the material generator locally (item 7); store outputs in the password manager; note fingerprints and expiry |
| 2 Create cluster | founder | PostgreSQL 17, Standard Edition, single node, region matching the app (`nyc` maps to `nyc1`, PR #326), in the VPC; create the dedicated database; save the cluster CA; **no standby, one node** |
| 3 Conformance (G2) | founder | temporary operator trusted-source rule, run the suite, remove the rule |
| 4 Promotion question (G3) | agent asks, founder answers | the AGENTS.md summary below; wait for an affirmative answer for that build |
| 5 Deploy | founder | apply the spec from a local filled copy (never committed); the first deployment's job runs before the app can be added as a trusted source, so add the rule **immediately** (`doctl databases firewalls append <cluster-id> --rule app:<app-id>`); redeploy once to confirm the job works with the rule in place |
| 6 Verify (G4) | founder, agent reads logs the founder shares | deployment phase, running components, `memory` health, then 7.5 and 7.6 |
| 7 Use | founder | the window of days; Charlie writes five metadata fields per synthesis |
| 8 Tear down (G5) | founder | below |

**Promotion summary (what AGENTS.md asks for before the question).** Commit SHA; verification status (offline tests, local rehearsal, retests, conformance record, drill record, each PASS or FAIL, with dates); the cost change (section 9: about $0.03 per hour while up, a few dollars per window, unconfirmed against an invoice); and the plain statement "Charlie only, five metadata fields, fail-open, empty at the start of this window, secrets visible to console users". Then: "Do you want to promote this dev build to Stage?"

**Teardown and nothing-left-billing checklist** (modeled on `deploy/digitalocean-probe/RUNBOOK.md` section 11, which the founder ran on 2026-11-12). Order: apps, then trusted-source rules, then the cluster, then local files.

```text
RUN  doctl apps delete <app-id> --force
RUN  doctl databases firewalls list <cluster-id>            # note rules, including any operator-address rule
RUN  doctl databases firewalls remove <cluster-id> --uuid <rule-uuid>
RUN  doctl databases delete <cluster-id> --force            # destroys its backups
RUN  rm -rf <filled local spec> <generated materials, unless kept in the password manager>
```

Verify nothing remains billing (every command must show no Stage-memory resource): `doctl apps list`, `doctl databases list`, `doctl databases firewalls list <cluster-id>` (should error: not found), `doctl balance get`, then the control panel's billing page after about an hour. Also confirm no operator-address rule remains and no container registry was created. The existing `viharbot-stage` app and its seven components are a separate decision: whether the whole app, or only the memory pieces, goes down at the end of a window is N1.

## 9. Cost

**Sources.** List prices recorded in `docs/stage-memory-platform-verification.md` and PR #326 (rows 3a, 3c, 8b, 8d), retrieved 2026-11-11, **UNCONFIRMED against an invoice**. The probe's cluster slug `db-amd-1vcpu-1gb` (15,360 MiB storage) has no confirmed price: the earlier $15.00 (docs page) and $15.15 (marketing page, $0.02254 per hour) figures were for a Standard 1 GiB plan whose slug could not be matched (row 3a). The probe cost was not read from an invoice either.

**Assumptions.** One extra `apps-s-1vcpu-0.5gb` component at $5.00 per month (the "extra $5-per-month-class component", row 8b); the bootstrap job billed only while it runs, treated as about 0 (row 8d); the database at the marketing hourly figure $0.02254 (a 1 GiB single node at $15.15 per month). The marketing hourly rate equals the monthly price divided by 672 hours (15.15 / 672 = 0.02254), so the same 672-hour basis is used for the component: 5.00 / 672 = $0.00744 per hour. Databases bill per started hour with a one-hour minimum (DOCUMENTED, row 3c); services per second with a one-minute minimum (row 8b). Existing Stage components (web and six agents) are not counted.

| Window | Memory component | Database | Total |
|---|---|---|---|
| per hour | $0.00744 | $0.02254 | **$0.030** |
| 24 hours | $0.18 | $0.54 | **$0.72** |
| 3 days (72 h) | $0.54 | $1.62 | **$2.16** |
| 7 days (168 h) | $1.25 | $3.79 | **$5.04** |
| if left running a full 28-day month (672 h) | $5.00 | $15.15 | $20.15 |

The throwaway conformance and retest cluster adds one to two billed hours at $0.02254 (about $0.02 to $0.05), because a database bills a full started hour. Neither figure includes build minutes, bandwidth or storage beyond the plan (UNVERIFIED), or the 28-day billing wording in row 8b.

**What the first invoice must confirm:** the real hourly and monthly price of `db-amd-1vcpu-1gb` (and what its 15 GiB of storage costs, if separate); that the database billed by the started hour; that the `memory` component billed by the second with the 28-day wording interpreted as expected; that the bootstrap job billed near zero; that nothing else (registry, extra bandwidth, a leftover trusted-source rule or cluster) appeared; and, after teardown, that the line items stopped growing. If the invoice disagrees with this table, the invoice wins.

## 10. Risks and residual limits

| Risk | Plain statement |
|---|---|
| Console visibility of secrets | Anyone with console access to the app, or the team, can read every variable in section 3, including the admin credentials from the binding. Lab-grade, accepted in ADR-006. |
| Single node, no standby | One database node. A node failure loses availability, and loses data back to the last backup. New Standard clusters cannot have standby nodes after 30 November 2026 anyway (DOCUMENTED, row 3d). Acceptable only because memory is metadata and fail-open. |
| 7-day backups, no selective deletion | `erase` removes live data now; backups hold the record for up to 7 days; destroying the cluster destroys its backups. Accepted for this scope (decision 4); revisit before anything sensitive is stored. |
| Certificate expiry | About yearly. Expiry silently disables memory (Charlie still answers). Mitigation: a start-up log of days remaining, and a calendar reminder owned by the founder (N5). |
| Platform rollback re-runs the job | Appears to (TESTED, inconclusive). Every bootstrap and migration step must be idempotent, and this is why. |
| Public-URL database host name | The injected host has no `private-` prefix (TESTED) although the job reached a private address. Documentation says only the private host name is restricted to the VPC (row 3l). The design relies on `verify-full`, the trusted-source rule and credentials, not on the name. |
| Trusted-source denial untested | Only the permitted app's connection was observed. Until 7.3 passes, do not claim the cluster refuses other clients. |
| Retry policy unverified | A platform retry of a failing job cannot be ruled out. |
| Reachability tested narrowly | A second app in the same account and region could not reach the service; other VPCs and accounts were not tested (row 2c). The A2 assertion check remains the control. |
| Memory is empty each window | By design. Nothing carries over, so memory cannot yet be evaluated as a long-term feature on Stage. |
| TLS depends on the application | Between Charlie and memory, and memory and the database, encryption and verification are the application's, not the platform's. A bug in the new launcher or client loader is the whole protection, so those items carry the strictest tests. |
| One day, one account | The probe tested one topology on one account on one day; platform behaviour can change. Nothing here should be read as a guarantee. |
| Runtime log retention | Runtime logs have no stated retention unless forwarded (row 10a); the content-free audit lines live there. Accepted for a lab window; decide before anything sensitive is stored. |

## 11. Decision register

### 11.1 Decided (founder, 2026-11-11; not reopened here)

1. **Stage TLS.** One self-signed certificate served by the memory service itself on an internal port; Charlie trusts that certificate as its only trust anchor; hostname verification stays on; the certificate and key are encrypted DigitalOcean variables; about one year validity, yearly renewal.
2. **Stage database.** DigitalOcean Managed PostgreSQL, Standard Edition, single node, PostgreSQL 17, one shared instance with one dedicated database and the existing `memory` schema, a service role and a separate migrator role; the cluster may be created and destroyed per window, so memory starts empty each window and bootstrap and migrations run on every fresh cluster.
3. **Stage secrets.** Encrypted DigitalOcean environment variables, visible to console users (the risk accepted in ADR-006): Charlie's key, the registry, the certificate and key, the cluster CA and the role passwords; admin credentials come from the database binding.
4. **Backups.** DigitalOcean's 7-day backups with no selective deletion are accepted for erase ("gone from live data now, from backups within 7 days"); revisit before any agent stores anything sensitive.
5. **Scope.** Charlie only, agent-private tier only, no feature flag (active when a memory URL is configured), best effort and fail-open.

Also settled: DigitalOcean is Stage, AWS and GCP are Production options; CI does not run PostgreSQL; nothing deploys without the founder's explicit promotion approval (AGENTS.md).

### 11.2 New decisions this design surfaced (all seven accepted as recommended by the founder, 2026-11-12)

| # | Question | Recommended answer | Why |
|---|---|---|---|
| N1 (DECIDED 2026-11-12: as recommended) | Create the cluster fresh for each window, and delete the whole `viharbot-stage` app too, or keep either between windows? | Recreate the cluster every window. Delete the memory pieces at least; delete the whole app only if you also want the other seven components off. | An idle single-node cluster costs about $15 a month for data you have decided not to keep, and destroying it also destroys its backups, which matches the erase story. The price is a small per-window checklist (new CA, new rule, new passwords). |
| N2 (DECIDED 2026-11-12: as recommended) | Who generates and holds the Stage materials and role passwords? | You, locally, with the generator, storing outputs in your password manager. No agent generates, sees or stores them. Regenerate passwords every window; certificate, signing key and registry yearly, or when you choose. | The values are visible to console users anyway, but they should never pass through chat, a repository, or an agent. |
| N3 (DECIDED 2026-11-12: as recommended) | Run the retest (7.3) before building the bootstrap (item 6)? | Yes, one short throwaway cluster, once, before item 6, and run the conformance suite on the same cluster if item 9 is ready. | Three open points (the migrator CONNECT grant, the `SET` grant, the trusted-source denial) change the bootstrap's SQL and the claims it may make. Testing costs a few cents; rewriting the bootstrap costs a review. |
| N4 (DECIDED 2026-11-12: as recommended) | May the conformance run add your own address as a temporary trusted source? | Yes, as an `ip_addr` rule on the throwaway or pre-app cluster, removed in the same session and checked in teardown. | The suite needs a direct connection; leaving the cluster open to anyone with credentials is worse. The address is never written into the repository. |
| N5 (DECIDED 2026-11-12: as recommended) | Who owns certificate renewal, and how often? | You, with a reminder at eleven months; or simply issue a new certificate every window and skip renewal. | Expiry silently turns memory off. A short-lived certificate per window makes expiry a non-event for this lab. |
| N6 (DECIDED 2026-11-12: as recommended) | Fix the migrator's database-level privilege by changing `migrate.py` or by granting `CREATE` on the database? | Change `migrate.py` to skip creating the schema when it exists (item 5). | It keeps the migrator unable to create other schemas, and it is one small, tested change. |
| N7 (DECIDED 2026-11-12: as recommended) | Where should the memory service's audit lines go during a window? | Leave them in runtime logs for now and read them with `doctl apps logs`; do not forward. | The lines are identifiers only. Forwarding adds a third-party destination and a trusted-source conflict (row 10b) for no benefit while only metadata is stored. Revisit before anything sensitive. |

**Recorded 2026-11-12.** The founder accepted the recommended answer to N1 to N7. In practice: the cluster is recreated every window; the founder alone generates and holds the materials and role passwords (database passwords fresh each window, certificate, signing key and registry yearly or on demand); the probe retest (7.3) runs once on a throwaway cluster before the bootstrap entry point (work item 6) is built; the conformance run may add the operator's own address as a temporary trusted source on the throwaway or pre-app cluster, removed in the same session and checked in teardown, never written to the repository; certificate renewal is the founder's, with a reminder at eleven months, or a fresh certificate each window; `migrate.py` skips creating the schema when it already exists (work item 5) instead of widening the migrator's database privileges; the memory service's audit lines stay in runtime logs and are not forwarded.

This changes the build order in section 6 as follows: items 1, 2, 3, 4, 5 and 7 do not depend on the retest and may proceed once this design is merged; item 6 (bootstrap and migrate entry point) waits for the retest result; item 11 (the Stage app spec) still needs the founder's explicit promotion approval for that build.

## 12. Glossary

- **Trusted source:** a rule on the database cluster that lets only named clients (here, the Stage app) connect and blocks every other connection.
- **Pre-deploy job:** a one-shot container App Platform runs before each deployment; if it fails, the deployment does not go ahead.
- **Pinned certificate:** a certificate the client trusts by itself, instead of trusting a public authority, so it accepts that one certificate and nothing else.
- **Self-signed certificate:** a certificate signed by its own key rather than by an authority; it proves nothing unless the client already holds a copy.
- **`verify-full`:** the database connection mode that checks both that the server's certificate is trusted and that its name matches; `require` encrypts but does not check who is on the other end.
- **Cluster CA:** the certificate of the authority that signs the database's certificate; the client needs it to verify the database.
- **Migrator role:** a database login that owns the memory tables and can change their structure; only the bootstrap job uses it.
- **Service role:** the database login the running memory service uses; it can read and write records but cannot change the structure.
- **Idempotent:** safe to run again with the same result, which matters because the platform may re-run the job.
- **Fail open:** when memory breaks, Charlie keeps answering without it instead of refusing.
- **Console visibility:** an encrypted variable is hidden in logs and the spec but can still be read by people with console access to the app.
- **Window:** the few days Stage is up, during which the cluster exists and memory fills; at the end the cluster and its data are destroyed.
