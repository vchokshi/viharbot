# Datastore platform: local, Stage and Production

**Status: proposal, with founder policy decisions recorded below (2026-11-10). Nothing here is implemented or provisioned.** No Compose service, DigitalOcean resource or AWS resource was added for this document, and `AGENTS.md` still forbids provisioning AWS resources without an explicit request. It exists so that the memory work (#40, ADR-009) and any later store are chosen from what runs the same way in all three environments, instead of from what one cloud happens to offer.

## Approved clouds and stores (founder, 2026-11-10)

- **Approved clouds:** DigitalOcean (Stage), and AWS and GCP as Production options. Approval is not permission to provision: `AGENTS.md` still requires an explicit request for AWS and GCP resources and a reviewed, confirmed promotion for Stage.
- **Approved stores:** everything in the portable set below, and DynamoDB.
- **Placement rule:** a store is placed in the cloud that offers it. If the store an agent needs is not offered in one approved cloud, it may be launched in another approved cloud (for example DynamoDB in AWS for an agent that runs on DigitalOcean or GCP). A cross-cloud placement must:
  - use authenticated TLS for every connection (never plain HTTP across a cloud boundary, and `ADR-006` already requires TLS across platform boundaries);
  - use credentials that belong to that environment only, held by the service that fronts the store, and never shared between Stage and Production;
  - keep Stage and Production data separate, since cross-environment data movement is still undecided and denied (ADR-009);
  - have its cost (including egress) and added latency reported before promotion, and any failure of the other cloud treated as a dependency of the agent;
  - stay behind the same service interface, so the agent can later move to a store in its own cloud without a contract change.
  Prefer the store that exists in the agent's own cloud when it meets the need; a cross-cloud store is a choice with a cost, not a default.
- **Memory:** unchanged. ADR-009's PostgreSQL decision stands; DynamoDB approval does not reopen it.

## Why this matters now

- Stage runs on DigitalOcean App Platform. Its container filesystem is not persistent: data on the host's local disk is lost after deployments and other container replacements, and DigitalOcean's own guidance is to use Managed Databases or Spaces for durable state ([App Platform limits](https://docs.digitalocean.com/products/app-platform/details/limits/), [How to store data](https://docs.digitalocean.com/products/app-platform/how-to/store-data/)). Today's JSONL memory at `/tmp/viharbot-memory` is therefore ephemeral on Stage.
- ADR-009 chose DynamoDB for Production and left the Stage store open. DigitalOcean has no DynamoDB, so a Stage that must honor the same memory contract needs a store that exists on both providers.
- The team is designed for roughly 8 to 12 agents. Per-agent isolation can be done with separate schemas, key prefixes or buckets on a small number of shared stores, so store count does not need to scale with agent count.

## The portable set

| Role | Local (Docker Compose) | Stage (DigitalOcean) | Production (AWS) | GCP | Verified in this session |
|---|---|---|---|---|---|
| Relational / transactional | PostgreSQL container | Managed PostgreSQL | RDS for PostgreSQL | Cloud SQL for PostgreSQL | All three providers list it ([DO](https://docs.digitalocean.com/products/databases/), [AWS decision guide](https://docs.aws.amazon.com/decision-guides/latest/decision-guides/databases-on-aws-how-to-choose.html)) |
| Relational (alternative) | MySQL container | Managed MySQL | RDS for MySQL | Cloud SQL for MySQL | Same sources |
| Key-value cache, queues, rate limits, `jti` replay cache | Valkey container (Redis-compatible) | Managed Valkey | ElastiCache (Valkey) | Memorystore (lists Valkey, Redis, Memcached) | DO lists Valkey as Redis-compatible; its API still uses "redis" and "Caching" labels. AWS lists ElastiCache with Valkey, Memcached and Redis OSS |
| Document | MongoDB container | Managed MongoDB | DocumentDB (MongoDB-compatible) | Firestore (serverless document; MongoDB compatibility reported) or MongoDB Atlas on GCP | DO and AWS list it; GCP's Firestore and Atlas listing from Google's database pages. DocumentDB is an API-compatible service, not MongoDB itself, so behavior differences must be tested |
| Search / vector later | OpenSearch container | Managed OpenSearch | Amazon OpenSearch Service | **Not verified** | DO and AWS list it; the GCP search found nothing, so no GCP equivalent is claimed |
| Event streaming (only if a real need appears) | Kafka container | Managed Kafka | Amazon MSK | **Not verified** | DO verified; GCP's managed Kafka was not confirmed either. **MSK was not confirmed in this session's search; check before relying on it.** `AGENTS.md` also forbids brokers unless the founder changes direction |
| Object storage (large bodies, artifacts) | S3-compatible container | Spaces (S3-compatible) | S3 | Cloud Storage (**not verified in this session**) | Spaces is already used for images. Local emulator not chosen, see below |
| Single-table NoSQL | DynamoDB Local | **None** | DynamoDB | Bigtable or Firestore (nearest, **not equivalent**: different consistency, transaction and key models) | AWS service verified. No DigitalOcean equivalent exists. Approved; for an agent outside AWS it is a cross-cloud placement under the rule above |

DigitalOcean lists six managed engines: PostgreSQL, MySQL, Valkey, MongoDB, Kafka and OpenSearch. Backups differ by engine: daily point-in-time backups for PostgreSQL, MySQL, MongoDB and OpenSearch, not for Valkey or Kafka. Kafka and OpenSearch have no standby nodes. Announced plan changes (15 and 30 November 2026) limit Standard Edition PostgreSQL and MySQL plans above 4 GiB RAM and standby or read-only nodes for new clusters; existing clusters are not affected. Check current plans and prices before choosing a size.

GCP's own managed set, from [Google's database overview](https://cloud.google.com/products/databases): Cloud SQL (MySQL, PostgreSQL, SQL Server), Spanner, Bigtable, Firestore and Memorystore, with MongoDB Atlas offered as a partner service. Spanner and Bigtable have no local container or DigitalOcean counterpart, so they fall under the same placement rule as DynamoDB: they run in GCP, and an agent elsewhere reaches them across clouds only under the cross-cloud conditions above. Prices, plans and regions are not checked.

## What can run in Docker Desktop

Everything in the first six rows runs as a standard container on a private Compose network, with a named volume for durability. Not verified here: exact image names and tags, resource use on a developer laptop, and licensing of each image. Confirm these when a store is actually added. One flag for the object store: the license and maintenance status of the best-known S3-compatible server image has changed over time, so compare current options (for example LocalStack, SeaweedFS or Garage) rather than assuming one.

Rules for any store added to Compose:

- Private network only, no published host port unless bound to `127.0.0.1`, same as the agents today.
- Credentials from environment or secrets, never in source, and not reused between environments.
- One database or schema per agent or namespace, with a separate credential where the store supports it, so the isolation tests in #48 have something real to test.
- A store is added only when a feature needs it, in line with `AGENTS.md` ("do not add databases without a concrete need").

## Decision this raised for ADR-009 (founder, resolved 2026-11-10: PostgreSQL everywhere)

Historical context, kept for the record. ADR-009 described DynamoDB conditional and transactional writes as the way to get its guard rules. Because Stage and local cannot run DynamoDB natively (DynamoDB Local is an emulator, and DigitalOcean has no equivalent), there are two honest options:

1. **PostgreSQL as the one memory backend everywhere** (container locally, Managed PostgreSQL on Stage, RDS on Production). One contract, one set of tests, real transactions. ADR-009's DynamoDB design would be amended, and ADR-009 is a founder-review path.
2. **Keep DynamoDB for Production**, and accept that Stage and local use a different engine behind the same interface. The ADR must then say which guarantees are weaker on Stage, and the contract tests must pass on both. This is the cost the ADR already warns about.

Recommendation: option 1, unless there is a reason to want DynamoDB specifically. It removes the Stage gap and the dual-backend test burden. This is a recommendation, not a decision.

## Not verified

- Image names, tags, sizes and licenses for each local container.
- MSK availability and any AWS plan or price.
- DigitalOcean App Platform persistent file storage: the docs describe both "no persistent storage" and an NFS mount guide, and these conflict. Treat persistent files on App Platform as unavailable until verified.
- Cost of any managed store. Provisioning any of them is a cost change that must be reported before promotion to Stage.
