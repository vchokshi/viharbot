# Stage memory platform verification (DigitalOcean)

**Status:** Research record, not a decision. Nothing was provisioned, no DigitalOcean API, `doctl` or credential was used, and no account was accessed.

**Why.** ADR-009 puts the governed memory service on PostgreSQL everywhere, with DigitalOcean Managed PostgreSQL on Stage and authenticated TLS for every non-loopback connection. Before the founder designs Stage memory and decides what to provision, each platform assumption needs a source in DigitalOcean's own documentation, and each gap needs to be visible.

**Method and limits.** Retrieved 2026-11-11 from `docs.digitalocean.com` (and one DigitalOcean marketing page, marked). Pages were read through a fetch tool that returns a model-written extract, not the raw page, so "evidence" below is a paraphrase unless in quotation marks, and quotes should be re-checked against the live page before anyone relies on them. Only DigitalOcean pages were used. A statement is VERIFIED only if a DigitalOcean page says it; an absent statement is NOT DOCUMENTED, never inferred. Several pages carry old "last updated" dates (stated per row). Prices are DigitalOcean's published list prices on the retrieval date and may change.

Base URL for all rows: `https://docs.digitalocean.com` unless a full URL is shown. "Retrieved" is 2026-11-11 for every row.

## Findings

Status key: V = VERIFIED, C = CONTRADICTED, N = NOT DOCUMENTED.

### 1. App Platform service-to-service traffic

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 1a | Is internal traffic encrypted in transit? | N | `/products/app-platform/how-to/manage-internal-routing/`, `/details/features/`, `/details/intro-faq/` | None of the pages state whether internal traffic is encrypted. The internal examples use plain `http://`, e.g. `http://web:3000`. Features page lists "Automatic SSL/TLS certificates (HTTPS)" for public traffic only. |
| 1b | Mutual TLS or workload identity offered? | N | same pages, `/details/features/` | No mention of mTLS or workload identity on any page read. |
| 1c | Internal routing preserves path, query and body bytes? | N | `/how-to/manage-internal-routing/` | Page says nothing about path, query or body handling. Examples stop at host and port. ADR-006 request binding cannot be assumed to survive a platform hop. |
| 1d | Does internal HTTPS work; does the component serve its own TLS or does the platform terminate it? | N | `/how-to/manage-internal-routing/` | Page says `internal_ports` "can accommodate any TCP-based traffic" (so a component could bind a TCP port), but nothing about HTTPS, certificates or termination for internal ports. Intro FAQ mentions certificates "stored in a CDN" without saying where TLS terminates. |

### 2. Internal (private) services

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 2a | Can a service expose a non-public internal port? | V | `/how-to/manage-internal-routing/`, `/products/app-platform/reference/app-spec/` | `internal_ports`: "The ports on which this service will listen for internal traffic." |
| 2b | Can a service be internal-only? | V | `/how-to/manage-internal-routing/` | Remove `http_port` and the ingress routes (with at least one internal port) to make a backend component "inaccessible from the public internet". Note: this page says "remove", other pages do not describe a "private service" type. |
| 2c | Reachable only by components of the same app? From a VPC or other apps? | N | `/how-to/manage-internal-routing/`, `/how-to/enable-vpc/` | Components of the same app reach a service over the app's LAN. Neither page says that only the same app can reach it, and neither says whether other apps or VPC resources can. The VPC page is silent on internal service-to-service traffic and on VPC-to-app direction. |
| 2d | Stable internal name per component? | V | `/how-to/manage-internal-routing/` | Components address a service by its name, e.g. `http://web:3000`. Stability across redeploys is not separately stated. |
| 2e | Do jobs and workers receive requests? | V | same | They cannot; they "can only send outbound requests". |

### 3. Managed PostgreSQL

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 3a | Entry plans and prices (Standard Edition) | V with a discrepancy | `/products/databases/postgresql/details/pricing/`; `https://www.digitalocean.com/pricing/managed-databases` (marketing page) | Docs page: single node "starts at $15.00 per month" for 1 GiB, HA "starts at $30.00 per month" for 2 GiB/1 vCPU primary plus at least one matching $30.00 standby, read-only nodes from $15.00, storage $0.215 per GiB per month. Marketing page table: 1 GiB/1 vCPU/10-30 GiB $15.15 per month ($0.02254 per hour); 2 GiB/1 vCPU/30-60 GiB $30.45; 4 GiB/2 vCPU/60-120 GiB $60.90; 8 GiB/4 vCPU/140-280 GiB $122.10; 16 GiB/6 vCPU/290-580 GiB $244.35. The $15.00 and $15.15 figures disagree between the two DigitalOcean pages; the marketing table also lists plans (8 and 16 GiB) that the plan-change announcement (3h) says new Standard clusters no longer get. The docs pricing page gave no vCPU or storage per plan. |
| 3b | Advanced Edition prices | V (partial) | `/products/databases/postgresql/details/pricing/` | Starts at $130.00 per month for 8 GiB/2 vCPU primary; standby and read-only nodes bill at the primary's rate; extra storage $0.115 per GiB per month. Full Advanced plan table not seen. |
| 3c | Per month or hourly? | V | `/products/app-platform/details/pricing/` | Databases "billed by the hour, with a one-hour minimum". Prices are quoted as monthly rates with an hourly equivalent. |
| 3d | Standby and read-only nodes | C (changing) | `/products/databases/postgresql/details/pricing/`, `/release-notes/upcoming/dbaas-plan-changes/`, `/details/limits/` | Announcement: "New clusters do not support standby nodes or read-only nodes" on Standard Edition. Applies to never-before PostgreSQL/MySQL accounts from 15 October 2026 and to all accounts from 30 November 2026. Advanced Edition supports both. Existing clusters are unaffected. The docs pricing page still lists Standard HA from $30.00. The limits page says each cluster is limited to 3 nodes. |
| 3e | Backups, point-in-time recovery, retention | V | `/products/databases/postgresql/how-to/restore-from-backups/`, `/details/limits/`, `/details/features/` | "backups run automatically once per day and are retained for seven days"; PITR "limited to the last 7 days". Restoring "creates a new copy of the cluster's primary node". |
| 3f | Backups and erasure | V (as implication) | `/how-to/restore-from-backups/` | Backups are retained seven days, so deleted rows remain restorable for that window. The page does not say individual backups can be deleted (N). "Destroying a database cluster destroys the backups of that database." |
| 3g | Supported PostgreSQL versions, is 17 available | V | `/products/databases/version-support-policy/` | Standard Edition 18, 17, 16, 15 available for new clusters; 17 available for new clusters until 8 May 2029, end of life 8 November 2029. Advanced Edition 18, 17, 16 have no announced end of life. 14 is unavailable for new clusters. |
| 3h | Plan changes dated 15 October and 30 November 2026 | V | `/release-notes/upcoming/dbaas-plan-changes/` | Standard Edition new clusters: no plans above 4 GiB RAM, no standby or read-only nodes; remaining Standard plans are Basic Shared CPU 1, 2 and 4 GiB. Advanced Edition adds Basic Shared CPU plans 8 to 64 GiB from 15 October. Existing clusters unaffected. Reference docs for API, Terraform, doctl were "still forthcoming". Limits page (last updated 2026-11-05) repeats the announcement. |
| 3i | Connection limits per plan | V | `/products/databases/postgresql/details/limits/` | Backend connections: 22 (1 GiB), 47 (2 GiB), 97 (4 GiB), 197 (8 GiB), 397 (16 GiB), 797 (32 GiB), 997 (64 GiB and above). |
| 3j | Connection pooling | V | `/details/limits/`, `/how-to/manage-connection-pools/` | PgBouncer, up to 21 pools per cluster. Default mode transaction; session mode is for "prepared statements, advisory locks, listen/notify"; mode cannot be changed after creation. The pooling page's connection count (25 per GiB, 3 reserved) differs from the limits table (22 at 1 GiB), and the page contradicts itself on pool size. Pool port not stated on these pages. |
| 3k | TLS to the database, sslmode, CA | V | `/how-to/connect/`, `/how-to/secure/` | "Managed PostgreSQL clusters require TLS for all connections." Connection details default to `sslmode=require` (encrypts, does not verify the server). `verify-full` is recommended. Standard Edition: download a CA certificate from the control panel. Advanced Edition: verifies against the system trust store, needs `sslrootcert=system` and libpq 16 or later. Features page says "SSL" for in transit. |
| 3l | Private (VPC) database hostname | V | `/how-to/connect/` | "Only resources in the same VPC network as the cluster can connect using the private hostname." |

### 4. Connecting App Platform to a cluster

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 4a | Attach a managed database to an app | V | `/products/app-platform/how-to/manage-databases/`, `/reference/app-spec/` | Control panel "Create or attach database" accepts an existing cluster. Spec `databases` entry has `engine`, `cluster_name` ("required for production databases"), `production`, `db_name`, `db_user`. Prose lists PostgreSQL 15 through 18 for the component. Referencing a cluster purely by connection string, without the database component, is not discussed (N). |
| 4b | VPC and private connectivity | V | `/how-to/enable-vpc/`, `/how-to/manage-databases/` | The database must be in the datacenter the app's region maps to (or a peered VPC). App with VPC access gets a VPC egress private IP, which trusted-source databases must allow. VPC access cannot be combined with dedicated egress IPs. |
| 4c | Trusted sources | V | `/how-to/manage-databases/` | "Enabling an app as a trusted source blocks any other connections." Connecting during the build with trusted sources enabled is not supported. |
| 4d | Credential injection | V | `/how-to/manage-databases/`, `/how-to/use-environment-variables/`, `/how-to/connect-pg-pools/` | Bindable variables such as `${db.USERNAME}`, `${db.PASSWORD}`, `${db.HOSTNAME}`, `${db.PORT}` and `DATABASE_URL`, injected at runtime; pool URL `${db.<pool>.DATABASE_URL}`. Component-name prefix limited to 32 characters. A CA certificate variable is documented only for Kafka; for PostgreSQL it is N. Pool page (updated 2023-01-06) does not mention CA or SSL. |
| 4e | Does the app's bindable `DATABASE_URL` use `verify-full`? | N | pages above | Not stated. Default connection details use `require`. |

### 5. Roles and bootstrap

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 5a | Create cluster users by control panel or API | V | `/products/databases/postgresql/how-to/manage-users-and-databases/` | Control panel "Add new user", `doctl databases user create`, `POST /v2/databases/{uuid}/users`; password reset and delete also available. User count limit not stated. |
| 5b | Can doadmin run arbitrary `CREATE ROLE` and what are its privileges | N for `CREATE ROLE` | `/how-to/modify-user-privileges/`, `/how-to/manage-users-and-databases/` | doadmin has "full access to every database you create". `GRANT`, `REVOKE`, `ALTER DEFAULT PRIVILEGES` and `ALTER ROLE ... NOCREATEDB` run as doadmin are shown. No page shows or forbids SQL `CREATE ROLE`. "Managed PostgreSQL does not allow superuser access." Limits page: users "cannot access the `superuser` role". |
| 5c | Role attribute behaviour | V | `/how-to/modify-user-privileges/` | On Advanced Edition new users get `CREATEDB`; "GRANT and REVOKE don't change role attributes". Default privileges apply only to tables created by the role that ran the command unless `FOR ROLE` is used. |
| 5d | Can the default database and user be removed | V | `/details/limits/`, `/how-to/manage-users-and-databases/` | No; "You cannot delete the default database and user." |

### 6. App Platform jobs

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 6a | Pre-deploy and post-deploy jobs exist | V | `/products/app-platform/how-to/manage-jobs/`, `/reference/app-spec/` | Triggers "Before every deploy", "After every successful deploy", "After every failed deploy". Spec kinds `PRE_DEPLOY`, `POST_DEPLOY`, `FAILED_DEPLOY`, `SCHEDULED`. |
| 6b | Failure of a pre-deploy job | V (indirect) | `/products/app-platform/getting-started/migrate-from-heroku/` | A pre-deploy job "blocks deployment if it fails". The how-to and reference pages do not state this. |
| 6c | Timeout and retry | V (timeout), N (retry) | `/products/app-platform/details/limits/` | Jobs: "The default timeout remains 30 minutes", configurable. Limits page last updated 2022-06-16. Retry behaviour not documented. |
| 6d | Does a pre-deploy job finish before the new service version serves? | N (directly) | `/details/intro-faq/` | FAQ says the previous instance keeps running "until the new instance is considered healthy". The jobs page does not state ordering relative to services. |
| 6e | Do jobs reach databases and internal services? | V | `/how-to/manage-internal-routing/` | Jobs can send outbound requests to services and the internet. Whether a job gets the same bindable database variables is not stated (N). |

### 7. Secrets and files

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 7a | Encrypted environment variables | V | `/how-to/use-environment-variables/`, `/reference/app-spec/` | Spec type `SECRET`: "the value will be encrypted on first submission". Encrypt option "stores it in the app spec" encrypted, hides values from logs, decrypted at runtime, and "can be visible to other team members who can access the application's console". Scopes `RUN_TIME`, `BUILD_TIME`, `RUN_AND_BUILD_TIME`. |
| 7b | Size limits for variables | N | pages above, `/details/limits/` | Not stated. |
| 7c | Mounting files (key registry, certificates) | N for secret files | `/how-to/use-environment-variables/`, `/how-to/mount-nfs-shares/` | No secret-file mount is documented; the environment-variables page says nothing about mounting files. NFS mounts exist only as a private preview (see 9). |
| 7d | Key management and rotation of the encrypted values | N | pages above | Not described. |

### 8. Health checks, sizes, minimum cost

| # | Question | Status | Source | Evidence |
|---|---|---|---|---|
| 8a | Health check types and defaults | V | `/how-to/manage-health-checks/`, `/reference/app-spec/` | TCP or HTTP (HTTP/1.1). Spec defaults: 0s initial delay, 10s period, 1s timeout, success 1, failure 9. Without `http_path` a TCP check is used. Liveness checks (restart) for services and workers. Whether health checks apply to internal-only services: N. |
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

## Implications for the memory design

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

1. Does App Platform encrypt traffic between components of one app, and is there any supported way to get mutual TLS or workload identity? Is plain `http://` to an internal port the only supported internal scheme?
2. Does internal routing preserve the request path, query string, headers and body bytes exactly?
3. Is an internal-only service reachable from other apps in the account, or from resources in the VPC, in either direction?
4. Can a PostgreSQL cluster's `doadmin` run `CREATE ROLE`, `CREATE SCHEMA`, and `ALTER DEFAULT PRIVILEGES ... FOR ROLE` as the init script requires, and which role attributes do API-created users receive on each edition?
5. What `sslmode` and CA does an app's injected `DATABASE_URL` use, and how does an app get the Standard Edition CA certificate (variable, file, or none)?
6. Does a failed pre-deploy job stop the new version from serving and leave the old version running? Retry policy? Do jobs receive database bindable variables?
7. Is there a size limit on encrypted variables, and any supported way to supply a certificate or key registry as a file?
8. Is the NFS private preview available to this account, in the Stage region, and what are its durability, backup and security terms?
9. Can individual database backups be deleted for erasure requests, and what happens to backups and WAL after a delete or a cluster destroy beyond what is quoted above?
10. Which Standard Edition or Advanced Edition plan applies to a new cluster on this account today (before and after 30 November 2026), and which one matches the $15.00 versus $15.15 discrepancy?
11. Which region does the Stage app map to, and is it one where the chosen edition and plan are offered?

## Notes on source disagreement

- Database entry price: $15.00 (docs pricing page) versus $15.15 (marketing pricing page).
- Database plan sizes: marketing page lists 8 and 16 GiB Standard plans; the announcement removes them for new Standard clusters.
- Persistent storage: limits and store-data pages (2022 and 2020) versus the NFS how-to (private preview).
- Log forwarding destinations: forwarding how-to versus app-spec reference.
- Connection counts: limits table versus pooling page.
- Pre-deploy failure behaviour is stated only in the Heroku migration guide.
