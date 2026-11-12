# Stage memory platform verification (DigitalOcean)

**Status:** Research record, not a decision, with one tested update. The research (2026-11-11) provisioned nothing and used no DigitalOcean API, `doctl` or credential. On 2026-11-12 the founder had the throwaway probe kit (`deploy/digitalocean-probe/`, merged in PR #324, merge commit `35d70fe`) run on a short-lived cluster and two apps, all destroyed within the hour. Results from that run are marked **T** and described in "Tested by throwaway probe" below. They are test observations on one account on one day, not DigitalOcean documentation.

**Why.** ADR-009 puts the governed memory service on PostgreSQL everywhere, with DigitalOcean Managed PostgreSQL on Stage and authenticated TLS for every non-loopback connection. Before the founder designs Stage memory and decides what to provision, each platform assumption needs a source in DigitalOcean's own documentation, and each gap needs to be visible.

**Method and limits.** Retrieved 2026-11-11 from `docs.digitalocean.com` (and one DigitalOcean marketing page, marked). Pages were read through a fetch tool that returns a model-written extract, not the raw page, so "evidence" below is a paraphrase unless in quotation marks, and quotes should be re-checked against the live page before anyone relies on them. Only DigitalOcean pages were used. A statement is VERIFIED only if a DigitalOcean page says it; an absent statement is NOT DOCUMENTED, never inferred. Several pages carry old "last updated" dates (stated per row). Prices are DigitalOcean's published list prices on the retrieval date and may change.

Base URL for all rows: `https://docs.digitalocean.com` unless a full URL is shown. "Retrieved" is 2026-11-11 for every row.

## Findings

Status key: V = VERIFIED (a DigitalOcean page says it), C = CONTRADICTED, N = NOT DOCUMENTED, **T = TESTED by the throwaway probe (observed, not documented)**. A row can carry both, for example `N / T PASS`.

### 1. App Platform service-to-service traffic

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 1a | Is internal traffic encrypted in transit? | N / T (service's own TLS only) | `/products/app-platform/how-to/manage-internal-routing/`, `/details/features/`, `/details/intro-faq/` | None of the pages state whether internal traffic is encrypted. The internal examples use plain `http://`, e.g. `http://web:3000`. Features page lists "Automatic SSL/TLS certificates (HTTPS)" for public traffic only. **Tested 2026-11-12 (probe):** Platform encryption of internal traffic is still undocumented and the app cannot observe it. The probe service terminated its own TLS on internal port 8443 and the client verified it, so encryption in transit between components is the application's own, as designed. |
| 1b | Mutual TLS or workload identity offered? | N | same pages, `/details/features/` | No mention of mTLS or workload identity on any page read. |
| 1c | Internal routing preserves path, query and body bytes? | N / T PASS | `/how-to/manage-internal-routing/` | Page says nothing about path, query or body handling. Examples stop at host and port. ADR-006 request binding cannot be assumed to survive a platform hop. **Tested 2026-11-12 (probe):** 18 of 18 echo cases arrived exactly as sent: method, raw target (query order, repeated keys, trailing `?`, hex case, `//` and `..` paths) and body SHA-256 for empty, small, 300 KB and non-ASCII bodies, repeated after redeploys. This covers only the topology where the service terminates its own TLS on an internal port; a platform proxy in front of a plain-HTTP internal port was not tested. |
| 1d | Does internal HTTPS work; does the component serve its own TLS or does the platform terminate it? | N / T PASS | `/how-to/manage-internal-routing/` | Page says `internal_ports` "can accommodate any TCP-based traffic" (so a component could bind a TCP port), but nothing about HTTPS, certificates or termination for internal ports. Intro FAQ mentions certificates "stored in a CDN" without saying where TLS terminates. **Tested 2026-11-12 (probe):** The service served HTTPS on internal port 8443 with a self-signed certificate delivered as encrypted base64 variables; the client, trusting only that certificate with verification on, verified it for the plain name `probe-service`. |

### 2. Internal (private) services

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 2a | Can a service expose a non-public internal port? | V | `/how-to/manage-internal-routing/`, `/products/app-platform/reference/app-spec/` | `internal_ports`: "The ports on which this service will listen for internal traffic." |
| 2b | Can a service be internal-only? | V / T PASS | `/how-to/manage-internal-routing/` | Remove `http_port` and the ingress routes (with at least one internal port) to make a backend component "inaccessible from the public internet". Note: this page says "remove", other pages do not describe a "private service" type. **Tested 2026-11-12 (probe):** A service with `internal_ports` and no `http_port` or route had no public route: `/__probe/health` and `/__probe/info` on the public URL returned the client's 404, and an HTTPS request to port 8443 on the public host got no response. A bare TCP connection to that port on the public host succeeds at the platform edge, but nothing answers behind it. |
| 2c | Reachable only by components of the same app? From a VPC or other apps? | N / T PASS (same account and region) | `/how-to/manage-internal-routing/`, `/how-to/enable-vpc/` | Components of the same app reach a service over the app's LAN. Neither page says that only the same app can reach it, and neither says whether other apps or VPC resources can. The VPC page is silent on internal service-to-service traffic and on VPC-to-app direction. **Tested 2026-11-12 (probe):** A second app in the same account and region (default VPC) could not resolve `probe-service` or the full `probe-service.app-<app-id>.svc.cluster.local` name, and TCP to the service's cluster address on 8443 timed out; its own summary reported `isolated: true`. Other VPCs and other accounts were not tested. |
| 2d | Stable internal name per component? | V / T PASS | `/how-to/manage-internal-routing/` | Components address a service by its name, e.g. `http://web:3000`. Stability across redeploys is not separately stated. **Tested 2026-11-12 (probe):** `probe-service` resolved to the same private address in every deployment checked (four client restarts) and verified each time. A second form, `<component>.app-<app-id>.svc.cluster.local`, also resolves inside the app but embeds the app ID (it changes if the app is recreated) and was not in the certificate. |
| 2e | Do jobs and workers receive requests? | V | same | They cannot; they "can only send outbound requests". |

### 3. Managed PostgreSQL

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 3a | Entry plans and prices (Standard Edition) | V with a discrepancy / T (slug differs) | `/products/databases/postgresql/details/pricing/`; `https://www.digitalocean.com/pricing/managed-databases` (marketing page) | Docs page: single node "starts at $15.00 per month" for 1 GiB, HA "starts at $30.00 per month" for 2 GiB/1 vCPU primary plus at least one matching $30.00 standby, read-only nodes from $15.00, storage $0.215 per GiB per month. Marketing page table: 1 GiB/1 vCPU/10-30 GiB $15.15 per month ($0.02254 per hour); 2 GiB/1 vCPU/30-60 GiB $30.45; 4 GiB/2 vCPU/60-120 GiB $60.90; 8 GiB/4 vCPU/140-280 GiB $122.10; 16 GiB/6 vCPU/290-580 GiB $244.35. The $15.00 and $15.15 figures disagree between the two DigitalOcean pages; the marketing table also lists plans (8 and 16 GiB) that the plan-change announcement (3h) says new Standard clusters no longer get. The docs pricing page gave no vCPU or storage per plan. **Tested 2026-11-12 (probe):** `doctl databases options slugs --engine pg` listed no `db-s-1vcpu-1gb`. The 1 GiB single-node options were `db-amd-1vcpu-1gb`, `db-amd-1vcpu-1gb-15-v5` and `db-intel-1vcpu-1gb`. The test used `db-amd-1vcpu-1gb`, which reported 15,360 MiB of storage; its price was not confirmed against an invoice. |
| 3b | Advanced Edition prices | V (partial) | `/products/databases/postgresql/details/pricing/` | Starts at $130.00 per month for 8 GiB/2 vCPU primary; standby and read-only nodes bill at the primary's rate; extra storage $0.115 per GiB per month. Full Advanced plan table not seen. |
| 3c | Per month or hourly? | V | `/products/app-platform/details/pricing/` | Databases "billed by the hour, with a one-hour minimum". Prices are quoted as monthly rates with an hourly equivalent. |
| 3d | Standby and read-only nodes | C (changing) | `/products/databases/postgresql/details/pricing/`, `/release-notes/upcoming/dbaas-plan-changes/`, `/details/limits/` | Announcement: "New clusters do not support standby nodes or read-only nodes" on Standard Edition. Applies to never-before PostgreSQL/MySQL accounts from 15 October 2026 and to all accounts from 30 November 2026. Advanced Edition supports both. Existing clusters are unaffected. The docs pricing page still lists Standard HA from $30.00. The limits page says each cluster is limited to 3 nodes. |
| 3e | Backups, point-in-time recovery, retention | V | `/products/databases/postgresql/how-to/restore-from-backups/`, `/details/limits/`, `/details/features/` | "backups run automatically once per day and are retained for seven days"; PITR "limited to the last 7 days". Restoring "creates a new copy of the cluster's primary node". |
| 3f | Backups and erasure | V (as implication) | `/how-to/restore-from-backups/` | Backups are retained seven days, so deleted rows remain restorable for that window. The page does not say individual backups can be deleted (N). "Destroying a database cluster destroys the backups of that database." |
| 3g | Supported PostgreSQL versions, is 17 available | V / T PASS | `/products/databases/version-support-policy/` | Standard Edition 18, 17, 16, 15 available for new clusters; 17 available for new clusters until 8 May 2029, end of life 8 November 2029. Advanced Edition 18, 17, 16 have no announced end of life. 14 is unavailable for new clusters. **Tested 2026-11-12 (probe):** A new Standard Edition (`pg`) cluster created with version 17 ran PostgreSQL 17.11. The engine list offered 15 to 18 for `pg` and 16 to 18 for `advanced_pg`. |
| 3h | Plan changes dated 15 October and 30 November 2026 | V | `/release-notes/upcoming/dbaas-plan-changes/` | Standard Edition new clusters: no plans above 4 GiB RAM, no standby or read-only nodes; remaining Standard plans are Basic Shared CPU 1, 2 and 4 GiB. Advanced Edition adds Basic Shared CPU plans 8 to 64 GiB from 15 October. Existing clusters unaffected. Reference docs for API, Terraform, doctl were "still forthcoming". Limits page (last updated 2026-11-05) repeats the announcement. |
| 3i | Connection limits per plan | V | `/products/databases/postgresql/details/limits/` | Backend connections: 22 (1 GiB), 47 (2 GiB), 97 (4 GiB), 197 (8 GiB), 397 (16 GiB), 797 (32 GiB), 997 (64 GiB and above). |
| 3j | Connection pooling | V | `/details/limits/`, `/how-to/manage-connection-pools/` | PgBouncer, up to 21 pools per cluster. Default mode transaction; session mode is for "prepared statements, advisory locks, listen/notify"; mode cannot be changed after creation. The pooling page's connection count (25 per GiB, 3 reserved) differs from the limits table (22 at 1 GiB), and the page contradicts itself on pool size. Pool port not stated on these pages. |
| 3k | TLS to the database, sslmode, CA | V / T PASS | `/how-to/connect/`, `/how-to/secure/` | "Managed PostgreSQL clusters require TLS for all connections." Connection details default to `sslmode=require` (encrypts, does not verify the server). `verify-full` is recommended. Standard Edition: download a CA certificate from the control panel. Advanced Edition: verifies against the system trust store, needs `sslrootcert=system` and libpq 16 or later. Features page says "SSL" for in transit. **Tested 2026-11-12 (probe):** The injected connection information had `sslmode=require`. `require` worked (TLS 1.3). `verify-full` and `verify-ca` with the cluster CA pasted as a base64 variable worked. `verify-full` against system roots failed (certificate verify failed), as the docs predict for Standard Edition. The CA is available from `doctl databases get-ca <cluster-id>` (prints PEM). |
| 3l | Private (VPC) database hostname | V / T (partial) | `/how-to/connect/` | "Only resources in the same VPC network as the cluster can connect using the private hostname." **Tested 2026-11-12 (probe):** The injected host name had the shape `***.db.ondigitalocean.com`, without a `private-` prefix, yet the job connected to a private 10.x address. |

### 4. Connecting App Platform to a cluster

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 4a | Attach a managed database to an app | V / T PASS | `/products/app-platform/how-to/manage-databases/`, `/reference/app-spec/` | Control panel "Create or attach database" accepts an existing cluster. Spec `databases` entry has `engine`, `cluster_name` ("required for production databases"), `production`, `db_name`, `db_user`. Prose lists PostgreSQL 15 through 18 for the component. Referencing a cluster purely by connection string, without the database component, is not discussed (N). **Tested 2026-11-12 (probe):** A spec `databases` entry with `cluster_name`, `production: true`, `db_name` and `db_user: doadmin` was accepted and worked. |
| 4b | VPC and private connectivity | V | `/how-to/enable-vpc/`, `/how-to/manage-databases/` | The database must be in the datacenter the app's region maps to (or a peered VPC). App with VPC access gets a VPC egress private IP, which trusted-source databases must allow. VPC access cannot be combined with dedicated egress IPs. |
| 4c | Trusted sources | V / T PASS | `/how-to/manage-databases/` | "Enabling an app as a trusted source blocks any other connections." Connecting during the build with trusted sources enabled is not supported. **Tested 2026-11-12 (probe):** `doctl databases firewalls append <cluster-id> --rule app:<app-id>` added the app as a trusted source; the job connected afterwards. |
| 4d | Credential injection | V / T (no CA variable) | `/how-to/manage-databases/`, `/how-to/use-environment-variables/`, `/how-to/connect-pg-pools/` | Bindable variables such as `${db.USERNAME}`, `${db.PASSWORD}`, `${db.HOSTNAME}`, `${db.PORT}` and `DATABASE_URL`, injected at runtime; pool URL `${db.<pool>.DATABASE_URL}`. Component-name prefix limited to 32 characters. A CA certificate variable is documented only for Kafka; for PostgreSQL it is N. Pool page (updated 2023-01-06) does not mention CA or SSL. **Tested 2026-11-12 (probe):** `${probe-db.DATABASE_URL}`, `${probe-db.HOSTNAME}`, `${probe-db.PORT}` and `${probe-db.USERNAME}` reached the job as environment variables. No platform-provided CA variable appeared among the database-related variable names, so the CA had to be pasted. |
| 4e | Does the app's bindable `DATABASE_URL` use `verify-full`? | N / T `require` | pages above | Not stated. Default connection details use `require`. **Tested 2026-11-12 (probe):** The injected URL carried `sslmode=require`. |

### 5. Roles and bootstrap

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 5a | Create cluster users by control panel or API | V | `/products/databases/postgresql/how-to/manage-users-and-databases/` | Control panel "Add new user", `doctl databases user create`, `POST /v2/databases/{uuid}/users`; password reset and delete also available. User count limit not stated. |
| 5b | Can doadmin run arbitrary `CREATE ROLE` and what are its privileges | N for `CREATE ROLE` / T PASS, one step needs a Stage change | `/how-to/modify-user-privileges/`, `/how-to/manage-users-and-databases/` | doadmin has "full access to every database you create". `GRANT`, `REVOKE`, `ALTER DEFAULT PRIVILEGES` and `ALTER ROLE ... NOCREATEDB` run as doadmin are shown. No page shows or forbids SQL `CREATE ROLE`. "Managed PostgreSQL does not allow superuser access." Limits page: users "cannot access the `superuser` role". **Tested 2026-11-12 (probe):** As `doadmin` (`createrole` and `createdb` true, `superuser` false) these succeeded: `CREATE ROLE ... LOGIN PASSWORD` for a migrator and a service role, `CREATE SCHEMA`, `ALTER SCHEMA ... OWNER`, `REVOKE` and `GRANT`, `ALTER DEFAULT PRIVILEGES`, creating a table as the migrator, the service role reading it through default privileges, and the service role being denied `CREATE` (SQLSTATE 42501). One step failed: the new migrator could not connect (`permission denied for database`), because the platform-created database is owned by `doadmin` and public CONNECT had been revoked. Compose avoids this by creating the database owned by the migrator. Stage needs an explicit `GRANT CONNECT` (or an owner change); that fix was not tested. |
| 5c | Role attribute behaviour | V | `/how-to/modify-user-privileges/` | On Advanced Edition new users get `CREATEDB`; "GRANT and REVOKE don't change role attributes". Default privileges apply only to tables created by the role that ran the command unless `FOR ROLE` is used. |
| 5d | Can the default database and user be removed | V | `/details/limits/`, `/how-to/manage-users-and-databases/` | No; "You cannot delete the default database and user." |

### 6. App Platform jobs

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 6a | Pre-deploy and post-deploy jobs exist | V / T PASS | `/products/app-platform/how-to/manage-jobs/`, `/reference/app-spec/` | Triggers "Before every deploy", "After every successful deploy", "After every failed deploy". Spec kinds `PRE_DEPLOY`, `POST_DEPLOY`, `FAILED_DEPLOY`, `SCHEDULED`. **Tested 2026-11-12 (probe):** A `PRE_DEPLOY` job ran on every deployment. |
| 6b | Failure of a pre-deploy job | V (indirect) / T PASS | `/products/app-platform/getting-started/migrate-from-heroku/` | A pre-deploy job "blocks deployment if it fails". The how-to and reference pages do not state this. **Tested 2026-11-12 (probe):** With the job set to exit 3 the deployment ended in ERROR at phase 7 of 13 and the old client kept serving (its start time unchanged). The platform then created an automated rollback deployment. |
| 6c | Timeout and retry | V (timeout), N (retry) / T retry INCONCLUSIVE | `/products/app-platform/details/limits/` | Jobs: "The default timeout remains 30 minutes", configurable. Limits page last updated 2022-06-16. Retry behaviour not documented. **Tested 2026-11-12 (probe):** The failed deployment's job log shows one run. Marker numbers 1 to 6 were contiguous with six rows, so one extra job run happened between the failed deployment and the next one, most likely the rollback deployment re-running the job. A platform retry cannot be ruled out. Logs of superseded deployments are refused (HTTP 400). |
| 6d | Does a pre-deploy job finish before the new service version serves? | N (directly) / T PASS | `/details/intro-faq/` | FAQ says the previous instance keeps running "until the new instance is considered healthy". The jobs page does not state ordering relative to services. **Tested 2026-11-12 (probe):** Three times the service's first health check found the marker the job had written (for example job marker 01:08:16, first health check 01:08:30). |
| 6e | Do jobs reach databases and internal services? | V / T PASS | `/how-to/manage-internal-routing/` | Jobs can send outbound requests to services and the internet. Whether a job gets the same bindable database variables is not stated (N). **Tested 2026-11-12 (probe):** The job received the database variables and connected. |

### 7. Secrets and files

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 7a | Encrypted environment variables | V / T PASS | `/how-to/use-environment-variables/`, `/reference/app-spec/` | Spec type `SECRET`: "the value will be encrypted on first submission". Encrypt option "stores it in the app spec" encrypted, hides values from logs, decrypted at runtime, and "can be visible to other team members who can access the application's console". Scopes `RUN_TIME`, `BUILD_TIME`, `RUN_AND_BUILD_TIME`. **Tested 2026-11-12 (probe):** `SECRET` variables holding base64 PEM values of 324 to 2,056 characters (certificate, key, CA, results token, cluster CA) worked when supplied through a filled local spec. Encryption on first submission and creating an app with empty secret values were not tested. |
| 7b | Size limits for variables | N / T partial | pages above, `/details/limits/` | Not stated. **Tested 2026-11-12 (probe):** Values up to about 2 KB worked. Larger sizes were not tested. |
| 7c | Mounting files (key registry, certificates) | N for secret files / T PASS (as variables) | `/how-to/use-environment-variables/`, `/how-to/mount-nfs-shares/` | No secret-file mount is documented; the environment-variables page says nothing about mounting files. NFS mounts exist only as a private preview (see 9). **Tested 2026-11-12 (probe):** The certificate, key and CA survived as encrypted base64 variables (`variable_form` base64). Mounting files remains undocumented and untested. |
| 7d | Key management and rotation of the encrypted values | N | pages above | Not described. |

### 8. Health checks, sizes, minimum cost

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 8a | Health check types and defaults | V / T partial | `/how-to/manage-health-checks/`, `/reference/app-spec/` | TCP or HTTP (HTTP/1.1). Spec defaults: 0s initial delay, 10s period, 1s timeout, success 1, failure 9. Without `http_path` a TCP check is used. Liveness checks (restart) for services and workers. Whether health checks apply to internal-only services: N. **Tested 2026-11-12 (probe):** A TCP health check on the internal-only service's port was accepted and the service became healthy. |
| 8b | Instance sizes and prices | V | `/products/app-platform/details/pricing/` | `apps-s-1vcpu-0.5gb` 1 shared vCPU, 512 MiB, $5.00 per month; `apps-s-1vcpu-1gb-fixed` 1 GiB $10.00; `apps-s-1vcpu-1gb` $12.00; `apps-s-1vcpu-2gb` $25.00; dedicated `apps-d-1vcpu-0.5gb` $29.00 up to `apps-d-8vcpu-32gb` $392.00. Services billed per second (one-minute minimum); "App components are billed for the first 28 days of the month after creation". |
| 8c | Development database | V | `/details/pricing/`, `/how-to/manage-databases/` | $7.00 per month per 512 MB dev database; "Dev database cluster permissions do not allow you to create additional databases" (limits page). Dev databases are for lightweight workloads; not a candidate for Stage per the page's own advice ("We recommend managed databases for production use"). |
| 8d | Published minimum for one extra service plus the database | V (list prices) | pricing pages above | One extra service from $5.00 per month, plus a Standard single-node PostgreSQL from $15.00 (docs) or $15.15 (marketing page) per month. This is a floor from list prices, not a recommendation. Any real Stage design (size, edition, standby) differs. Migrator job billed only while it runs. |

### 9. Persistent storage on App Platform

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 9a | Local disk is ephemeral | V | `/products/app-platform/details/limits/` (last updated 2022-06-16), `/how-to/store-data/` (2020-10-06) | "Data in the host instance's local filesystem is permanently lost after deployments and other container replacements." Limited to 4 GiB. |
| 9b | Volumes | C | same pages vs `/how-to/mount-nfs-shares/` | Limits page: "App Platform does not support volumes." Store-data page: network volumes cannot yet be mounted persistently. These two pages are older. |
| 9c | NFS shares | V (private preview) | `/products/app-platform/how-to/mount-nfs-shares/` | "This feature is in private preview." Spec `volumes` section with `type: NFS`, mounted into services, workers and jobs; one app per share; same team and datacenter as the app; not in `nyc`; mounting components run as UID/GID 65534; "may not be suitable for production workloads"; error for accounts without access: "NFS volume support is not enabled for your account." |
| 9d | Resolution | | | The conflict is a documentation-age conflict, not two live product positions. The older limits and store-data pages say no volumes; the newer NFS page documents a private-preview mount. Net effect for design: persistent mounts are not generally available, and the documented persistence options remain Managed Databases and Spaces. Whether the NFS preview is open to this account, and its durability and backup terms, are N. |

### 10. Logging and metrics

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 10a | App Platform log retention | V (partial) | `/products/app-platform/how-to/view-logs/` | Build and deploy logs "are retained for 90 days". No period for runtime logs: "To retain runtime logs, configure log forwarding to an external provider." Crash logs only through `doctl`. |
| 10b | App Platform log forwarding destinations | C | `/how-to/forward-logs/` vs `/reference/app-spec/` | Forwarding page lists DigitalOcean Managed OpenSearch, OpenSearch, Datadog and Better Stack. App spec reference lists `papertrail`, `datadog`, `logtail`, `open_search`. The two pages disagree on Papertrail. Forwarding to Managed OpenSearch with trusted sources enabled "is not supported"; dedicated egress IPs cannot be used for log forwarding. |
| 10c | Database log forwarding and retention | V (partial) | `/products/databases/postgresql/how-to/forward-logs/` | "PostgreSQL supports forwarding to OpenSearch, Elasticsearch, and rsyslog"; Datadog through rsyslog. No DigitalOcean-side retention stated; OpenSearch and Elasticsearch sinks have a configurable "maximum days to store logs". Viewing logs in the control panel is not covered on the page. |
| 10d | Metrics | V (existence only) | `/products/databases/postgresql/how-to/` index; `/details/features/` | Database monitoring and alert pages exist; App Platform features list "Per minutes application metrics". Retention not read. |
| 10e | Content-free audit logging guarantee | N | pages above | No page says what the platform logs of request content or whether request bodies appear anywhere. Content-free logging stays the application's responsibility. |

### 11. Anything that makes ADR-009 impossible or weaker

See the next section. No row above shows that ADR-009 is impossible on DigitalOcean. Several rows show that required properties are unconfirmed.

## Tested by throwaway probe (2026-11-12)

**What this is.** The founder ran `deploy/digitalocean-probe/RUNBOOK.md` on 2026-11-12 (about 01:01 to 01:20 UTC), driving it with `doctl` on the founder's account, kit merge commit `35d70fe`. The cluster and both apps were destroyed afterwards and a nothing-left-billing check was made. Region `nyc` (app) and `nyc1` (database), Standard Edition (`pg`) PostgreSQL 17.11, one node, slug `db-amd-1vcpu-1gb`, one app with a client, an echo service and a pre-deploy job, and a second one-worker app. **This is observation on one account on one day, not DigitalOcean documentation, and it can change.** No secret, token, URL, certificate or raw log is recorded here.

| # | Fact | Result | Evidence (non-secret) |
|---|---|---|---|
| 1 | Role bootstrap as `doadmin` | PASS, one step needs a Stage change | See row 5b. `createrole` and `createdb` true, `superuser` false. Migrator login failed for lack of CONNECT on a `doadmin`-owned database. |
| 2 | Internal calls arrive unchanged | PASS | 18 of 18 echo cases exact, `verified-tls` transport (row 1c). |
| 3 | Internal hostname and TLS with a pinned self-signed certificate | PASS | `probe-service:8443` resolves, connects and verifies. `probe-service.app-<app-id>.svc.cluster.local` resolves and connects but is not in the certificate. Certificate and key worked as base64 encrypted variables. |
| 4 | Reachability | PASS | Second app in the same region: no name resolved, private address timed out, public URL did not answer as the service (`isolated: true`). Laptop: no internal route; port 8443 on the public host accepts TCP at the edge but returns nothing. |
| 5a | Job receives database variables | PASS | `PROBE_DB_URL`, hostname, port and username present. |
| 5b | Ordering | PASS | Marker present at the service's first health check on three deployments. |
| 5c | Re-run safety | PASS | Run 2 exit 0, `rerun_detected` true, roles altered not re-created. |
| 5d | Failing job blocks the deploy | PASS | ERROR at phase 7 of 13, old version kept serving, automated rollback followed. |
| 5e | Retry behaviour | INCONCLUSIVE | One job run seen in the failed deployment; an extra run number between it and the next deployment is most likely the rollback re-running the job. |
| 6a | Injected `sslmode` | `require` | No `private-` host prefix, but a private address. |
| 6b | `sslmode=require` works | PASS | TLS 1.3. |
| 6c | `verify-full` with the pasted CA works | PASS | `verify-ca` also works; `verify-full` against system roots fails. |
| 6d | PostgreSQL server version | 17.11 | |

**Findings that are not rows**

- `db-s-1vcpu-1gb` is no longer an offered PostgreSQL slug on this account (row 3a). The kit's runbook and the verification document's earlier text expected it.
- `doctl databases create` has no `--format` flag. Creating with `--wait` was not used; the cluster took about three minutes to reach `online`.
- Working as the runbook guessed: `doctl databases get-ca`, `doctl databases firewalls append --rule app:<id>` and `remove --uuid`, `doctl apps spec validate`, `doctl apps update --spec`, `doctl apps create-deployment`, and job logs under both `--type deploy` and `--type run`. `doctl apps logs --deployment <id>` is refused for superseded deployments (HTTP 400).
- App Platform leaves components whose configuration did not change running on a spec update; a manual deployment restarts everything.
- A failed pre-deploy job triggers an automated rollback deployment that completes in seconds without a rebuild.
- The cluster's private address stayed stable across deployments in this run.
- Cost was not read from an invoice. The cluster lived about 19 minutes (billed per started hour, so one to two hours at the recorded list price) and the account's month-to-date usage had not yet moved when checked.

### Proposed follow-ups for the founder (proposals only; not applied)

`docs/adr/` needs a founder-reviewed merge and governance expansion is paused, so these are proposals, not edits.

1. **Roles (ADR-009 mapping, Stage bootstrap).** The service and migrator role model works on a managed cluster. The Stage bootstrap must differ from the Compose init script: the database is created by the platform (owned by `doadmin`), so grant the migrator CONNECT, or change the owner, before it logs in. Test that fix on the next throwaway cluster before relying on it.
2. **Database TLS.** Authenticated TLS to the database is achievable on Stage Standard Edition: use `verify-full` with the cluster CA supplied as an encrypted variable. The platform's injected default (`require`) does not authenticate the server and should not be used as is.
3. **Web to memory TLS.** The founder's single self-signed certificate design is confirmed workable. Use the plain component name as the only hostname (and the certificate's name list), not the full cluster-style name, which embeds the app ID. Certificate renewal (about yearly) is an operational task.
4. **Reachability.** Application-level assertion checks remain the control. Isolation was confirmed only from another app in the same account and region; other VPCs and accounts are untested.
5. **Migration job.** A pre-deploy job is a viable migration step: ordering held, re-runs were safe, and a failure blocked the deploy and rolled back. Because the rollback appears to re-run the job, migrations must be idempotent. Keeping a schema-version check in the service is cheap defence in depth.
6. **Price and slug.** Confirm the price of `db-amd-1vcpu-1gb` (15 GiB storage) on the first invoice before the Stage cost summary quotes it; the earlier $15.00 and $15.15 figures were for a Standard 1 GiB plan whose slug could not be matched.
7. **`docs/memory-service.md`.** Update its Stage statement only when the founder decides to build Stage memory. These results do not by themselves make Stage ready or complete any gate.

## Implications for the memory design

*Several of the points below were partly settled by the probe; see "Tested by throwaway probe". The text is kept as written on 2026-11-11.*

- **TLS between web and memory service on Stage is not provided by anything the docs state.** ADR-009 requires authenticated TLS for every non-loopback connection, including private networks. The docs do not say App Platform encrypts internal traffic, offer mTLS or workload identity, or describe internal HTTPS. A design that meets the ADR therefore has the memory service terminate its own TLS on an internal port (the docs allow any TCP traffic on an internal port) with certificates the application supplies and callers verify. That is a design choice the founder would own, not a platform feature, and it still needs a certificate source (7c: no documented file mount; certificates would travel as encrypted variables, size limit unknown).
- **Request binding (ADR-006) cannot rely on the platform proxy.** Path, query and body preservation are undocumented. If the memory service terminates TLS on an internal port, there is no platform-defined hop to question, but this must be tested rather than assumed.
- **Signed service assertions and the key registry.** No secret-file mount is documented. Key storage on Stage reduces to encrypted environment variables (visible to team members with console access) or a store the service reads itself. Both are open questions in `docs/service-authentication.md`.
- **Database TLS is stronger than the web-to-memory hop.** The cluster requires TLS and `verify-full` is available and recommended, but the Standard Edition needs a downloaded CA file (which has to reach the container, with no documented file-mount path) and Advanced Edition needs libpq 16 or later and `sslrootcert=system`. Whether the memory service's database driver can verify the certificate must be decided before choosing the edition. App Platform's injected `DATABASE_URL` defaults to `require`, which does not authenticate the server.
- **Roles.** Users can be created through the control panel and API, and doadmin can GRANT and REVOKE. SQL `CREATE ROLE` is undocumented, superuser is not available, and role attributes (`CREATEDB`) differ by edition. The Compose init script (service role plus separate migrator role) cannot be assumed to run unchanged. Ownership of tables created by the migrator, and default privileges for the service role, need `FOR ROLE` handling as the privileges page shows.
- **Migration job.** Pre-deploy jobs exist and, per the Heroku guide only, block the deploy on failure. Ordering relative to the new service version and retry behavior are undocumented. Compose's one-shot migrate job maps onto a pre-deploy job conceptually, not provably. Job access to the database variables is undocumented.
- **Backups and erasure.** Backups are retained seven days and PITR is limited to seven days; destroying the cluster destroys backups. Selective deletion of backups is not documented. The `erase` contract in ADR-009 cannot reach into backups, so the documented guarantee has to be stated as erased from live data immediately and from backups within seven days, and the founder should decide whether that is acceptable.
- **High availability.** From 30 November 2026, new Standard Edition clusters cannot have standby or read-only nodes. A Stage memory database with a standby needs Advanced Edition (from $130.00 per month for the smallest listed plan), or runs single-node with the documented "automatic failover" but no standby. This changes the cost floor in 8d.
- **Versions.** PostgreSQL 17 (and 18) are available on both editions, so a PostgreSQL 17 target stays possible. App Platform's database component lists 15 through 18.
- **Persistence.** The memory service must not use local disk or the NFS preview for records. The database is the persistence layer, which matches ADR-009.
- **Logging.** Runtime logs have no stated retention unless forwarded; forwarding destinations are third-party or OpenSearch. Content-free audit logs must be produced by the service itself, and the founder must pick where they go.
- **Isolation and exposure.** "Only the same app" reachability of an internal service is not stated, so network isolation of the memory service from other apps and VPC resources cannot be assumed; the application-level assertion check remains the control.

## Open questions that only an account owner or DigitalOcean support can answer

Answered by the probe on 2026-11-12 (kept out of this list): whether internal routing preserves path, query and body (row 1c); whether `doadmin` can run `CREATE ROLE` and the related grants (row 5b); what `sslmode` the injected URL carries and how to get the Standard CA (rows 3k, 4d, 4e); whether a failed pre-deploy job blocks the deploy, and whether the job gets the database variables (rows 6b, 6e); reachability from another app in the same account and region (row 2c); which region the app maps to (`nyc` maps to `nyc1` and `nyc3`).

Still open:

1. Does App Platform itself encrypt traffic between components, and is there any supported mutual TLS or workload identity? (The probe showed the service's own TLS works; it cannot show what the platform does underneath.)
2. Is an internal-only service reachable from other VPCs or other accounts, or from VPC resources? (Same account and region was tested.)
3. What is the retry policy for a failed pre-deploy job, and does an automated rollback deployment re-run the pre-deploy job? (The probe was inconclusive.)
4. Is there a size limit on encrypted variables beyond the roughly 2 KB tested, and any supported way to supply a certificate or key registry as a file?
5. Is the NFS private preview available to this account and the Stage region, and what are its durability, backup and security terms?
6. Can individual database backups be deleted for erasure requests, and what happens to backups and WAL after a delete or a cluster destroy beyond what is quoted above?
7. Which Standard Edition plan does `db-amd-1vcpu-1gb` correspond to in the published price tables, what does the 15 GiB of storage cost, and what does a new cluster get on this account after 30 November 2026?
8. Does `GRANT CONNECT ON DATABASE ... TO <migrator>` (or `ALTER DATABASE ... OWNER TO <migrator>`) work as `doadmin` on a platform-created database? (Not tested.)
9. Does the platform-provided private-URL variable for a component exist, and under what name? (The kit left it unused.)

## Notes on source disagreement

- Database entry price: $15.00 (docs pricing page) versus $15.15 (marketing pricing page).
- Database plan sizes: marketing page lists 8 and 16 GiB Standard plans; the announcement removes them for new Standard clusters.
- Persistent storage: limits and store-data pages (2022 and 2020) versus the NFS how-to (private preview).
- Log forwarding destinations: forwarding how-to versus app-spec reference.
- Connection counts: limits table versus pooling page.
- Pre-deploy failure behaviour is stated only in the Heroku migration guide (now also observed, see row 6b).
- The documented PostgreSQL slug in `doctl` help (`db-s-1vcpu-1gb`) is not an offered slug for a new cluster on this account (see row 3a).
