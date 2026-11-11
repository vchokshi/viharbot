# Service authentication (ADR-006, step A2)

**Status: implemented on all eight agent services (Research, Security, Fact Check, Source Quality,
Privacy Review, Synthesis, Sage and Pixel), behind a setting that is off by default in code.** Local
Docker Compose turns it on by default (see "Turning it on"); Stage and the committed DigitalOcean
spec do not. This is authentication only. It proves which service signed a request and that the
request was not altered or replayed. It does **not** prove the caller is authorized by a delegation
grant: that is A3 (issue #95). Design: `docs/adr/ADR-006-workload-identity-and-authorization.md`.
Code: `src/viharbot/service_auth.py`.

## What it does

A callee verifies the `X-Viharbot-Assertion` header (a compact JWT, EdDSA only). It checks the
signature against a public key looked up by `kid` in a local registry file, `typ`, that `iss` and
`sub` equal the subject the registry binds to that `kid`, the audience (the service's own name, for
example `fact-check`), lifetime (at most 60 s, 10 s clock skew), that `cap` is in the registry
entry's capabilities, the method, canonical target and SHA-256 of the body bytes as received, and a
per-process `jti` replay cache (bounded, 10,000 entries; when full it refuses rather than evicts).
Headers `jku`, `x5u`, `x5c` and `jwk` are never read. Identity in a body or in other headers is
never used. Failures return 400 (bad target), 401, 403 (capability), or 503 (registry too stale) with
a generic body. Decisions are logged as JSON (request ID, caller, path without the query, reason,
never a token, key or body).

## Protected routes and capabilities

Every non-health route of every agent service verifies the assertion when `VIHARBOT_SERVICE_AUTH`
is on. Each route family has one capability. A route accepts only its own capability (403 for any
other), and the registry entry for the caller's key must also list it. Capabilities are
authentication scopes for a route family, not delegation grants.

| Service (the `aud`) | Protected routes | Capability |
| --- | --- | --- |
| `fact-check`, `source-quality`, `privacy-review`, `synthesis` | `POST /process`, `POST /invocations` | `specialist.process` |
| `research` | `POST /research`, `POST /research-and-assess`, `POST /invocations` | `research.run` |
| `security` | `POST /assess`, `POST /invocations` | `security.assess` |
| `sage` | `POST /process` | `sage.process` |
| `pixel` | `POST /process` | `pixel.process` |
| `pixel` | `GET /decks/{deck_id}.pptx`, `GET /decks/{deck_id}.html` | `pixel.read_deck` |

Left open on every service: `/health`, `/ping` (Research, Security and the specialists) and
`/model`. `/model` returns only the configured model alias, is what the web UI uses to show who uses
what, and was already open on the specialists; this change keeps that. Protecting it is a
follow-up: the web tier would sign a `GET /model` and would need a capability for it.

Capability names are registry data, so the change is additive: `specialist.process` is unchanged and
an existing registry keeps working. A registry that should call the new routes adds the new names to
the relevant key. Least privilege in the development registry: `web` holds every capability,
`research` holds only `security.assess` (it calls Security), and the `demo`-profile `orchestrator`
holds the specialist, Research and Security capabilities.

## Turning it on

Off by default in code, so Stage and the existing tests are unchanged. To enable one service:

- `VIHARBOT_SERVICE_AUTH=on`
- `VIHARBOT_KEY_REGISTRY=/path/to/registry.toml`, a mounted file holding **public keys only**.

The service refuses to start if the registry is missing, malformed or empty (for Research, Security,
Sage and Pixel as for the specialists). With authentication on, a body-size cap (below) also covers
the protected POST routes of the four services added in #235.

**Local Compose turns it on by default** (issue #235; Compose is the dev environment). Compose sets
`VIHARBOT_SERVICE_AUTH` and `VIHARBOT_OUTBOUND_AUTH` to `on` unless you override them in `.env`.
Generate development keys once, then start the stack as usual:

```bash
python scripts/dev_service_auth.py     # writes data/dev-auth/ (gitignored); needs only `cryptography`
docker compose up --build -d
```

The script writes `registry.toml` (public keys) and one private seed each for `web`, `research` and
`orchestrator`. Compose mounts the registry read-only into each verifying service and mounts each
private seed only into the service that signs with it (`web`, `research`, and the `demo`-profile
`orchestrator`). No key, seed or registry is in source or an image; `data/` is already gitignored.
If the files are missing, Compose creates empty directories in their place and the services refuse to
start (fail closed), so run the script first. To run without authentication set
`VIHARBOT_SERVICE_AUTH=off` and `VIHARBOT_OUTBOUND_AUTH=off` in `.env`. The seeds are mode 0644 so the
unprivileged container user can read them; they are development keys for a loopback-only stack.

**Stage is deliberately not changed.** `deploy/digitalocean/app.yaml` does not set either variable, so
Stage verifies and signs nothing. Stage key storage and distribution are open questions in ADR-006 and
a Stage change needs the "promote this dev build to Stage" confirmation. The trigger to do it is
recorded in `docs/deployment-strategy.md`.

Registry format:

```toml
[[keys]]
kid = "web-2026-11"          # unique
sub = "web"                  # the one subject this key may assert
public_key = "<base64url of the 32 raw Ed25519 public-key bytes>"
capabilities = ["specialist.process"]   # route capabilities this key may assert (table above)
```

Two entries for one `sub` give a rotation overlap; removing a `kid` retires it. The file is re-read
every 60 s and once (at most every 10 s) on an unknown `kid`. After a failed refresh the last-known
keys work for 15 minutes, then every request is refused (503).

A caller mints a token with `service_auth.mint_assertion(...)`, which loads the private key at call
time from a file path or `VIHARBOT_SIGNING_KEY` (PEM or base64url seed). Keys are never in source;
inject them from the platform secret store.

## Outbound assertions (A2b)

**Status: implemented for plain HTTP calls through `demo.post_json`, off by default.** Code:
`src/viharbot/outbound_auth.py`.

**Issuer location:** option A is accepted; see "Issuer-key location" below. `AssertionIssuer` is a
one-method interface and `InProcessIssuer` is its only implementation.

Each call mints one token from the actual request: `mth` (POST), `tgt` (the URL's path and query as
sent, canonicalized by `service_auth.canonical_target`), `req_sha256` (the exact body bytes),
`aud` (the destination service name), `cap` (the capability that service's route requires),
`exp` 30 s after `iat`, a fresh `jti`. It is sent in `X-Viharbot-Assertion`, never `Authorization`.

Configuration (all unset means off; requests are byte-for-byte unchanged):

- `VIHARBOT_OUTBOUND_AUTH=on`
- `VIHARBOT_SIGNING_KID`: the `kid` the callee registry holds for this service.
- `VIHARBOT_SERVICE_SUBJECT`: the `sub` (default `web`).
- `VIHARBOT_SIGNING_KEY_FILE` (a path) or `VIHARBOT_SIGNING_KEY` (PEM or base64url seed), read on
  every call and never logged.

When on and the key is unreadable, the kid is missing, or signing fails, the call raises and nothing
is sent (fail closed; no unauthenticated fallback).

Wired:

- Every `demo.post_json` call to `fact-check`, `source-quality`, `privacy-review`, `synthesis`
  (`specialist.process`), `research` (`research.run`), `security` (`security.assess`), `sage`
  (`sage.process`) and `pixel` (`pixel.process`). This covers the fixed pipeline, the iterative and
  identity-execution paths and the Pixel stage, because they use `post_json`. Sage has a client: the
  web tier offers Sage to Charlie when `SAGE_URL` is set (`web_app.agent_urls`) and reaches it through
  `post_json` with destination `sage`. (An earlier version of this document said no Sage client
  existed; that was stale.)
- Research to Security (`security_client.send_findings_to_security`, capability `security.assess`).
  Research signs with **its own** key and subject: the call uses `VIHARBOT_SIGNING_KID`,
  `VIHARBOT_SIGNING_KEY_FILE` and `VIHARBOT_SERVICE_SUBJECT` from the Research process, whose subject
  defaults to `research` (not `web`). A key registered for `web` cannot be used for it: the verifier
  binds each `kid` to one subject. Each service holds its own identity (ADR-006).
- The deck download (`pixel_execution.fetch_deck`, `GET /decks/{deck_id}.pptx|.html`, capability
  `pixel.read_deck`). It is a GET with an empty body: the assertion binds the method, the exact path
  and the SHA-256 of the empty body, so a token for one deck is unusable for another deck or format.

When minting is on and signing fails, none of these paths falls back to an unsigned request:
`post_json` raises, Research reports a Security communication failure (502), and `fetch_deck`
returns "missing".

Not wired:

- AgentCore (`agentcore://`, and `SECURITY_AGENTCORE_ARN` for Research to Security): untouched; the
  SigV4-plus-assertion carrier is separate work.
- `GET /model`, which stays open.
- Redirects: a signed request is sent through the no-redirect opener (`litellm_client.urlopen`), so the token is never forwarded to another origin; a redirecting callee surfaces as the 3xx `HTTPError`, and a loopback test with a real 302 asserts the target is never contacted. Unsigned calls keep the default behavior.
- `eng` and `cls` are fixed placeholders (`unscoped`, `unclassified`); there is no `dlg` (A3).
- No rotation tooling, no separate signer, no per-user identity.
- ADR-006's text still recommends option B before any AWS deployment; a follow-up must update it (see below).

### Issuer-key location (founder decision, 2026-11-03)

Option A (ADR-006) is accepted: the web process holds the signing key and mints assertions. This is a
lab. The same holds if the web service runs in AWS, where the key would be stored in AWS Secrets
Manager and injected into the web service (not verified or provisioned here). Keys are separate per
environment: a Stage key must never be accepted in production. Rotation is every 30 to 90 days with
an overlap window, per ADR-006. The `AssertionIssuer` interface is kept so a separate signer
(option B) can replace `InProcessIssuer` later.

What this does not change: a secret store protects storage and rotation, but the web process still
reads the key at runtime, so compromise of the web process still lets an attacker mint assertions for
every agent. It also relaxes ADR-006's recommendation to move to option B before any AWS deployment.
The ADR text is not updated in this PR; a follow-up is needed.

## Known limitations of A2

- **A verifying service needs signed callers.** Enabling authentication on a service works only
  while its callers also have outbound minting on and a key its registry lists. In Compose both are
  on together; turning only one off breaks calls (they fail closed).
- All eight services protect every non-health route (table above). `/health`, `/ping` and `/model`
  stay open. Follow-up: `/model`.
- `dlg` is optional and ignored; there is no grant, scope, redelegation or engagement/classification
  check (A3, #95, #94). `eng` and `cls` are required but not enforced. The audit claim is exactly:
  **every call to a protected route is authenticated while authentication is enabled** on that
  service. It is not "every call is authenticated" and not "every call is authorized". Routes left
  open (see above), any service with authentication off (every service on Stage today) and the
  AgentCore carriers make no such claim.
- No key-rotation tooling and no separate signer (option A is accepted; `AssertionIssuer` is the
  seam). The development key script creates keys once and does not rotate them.
- The AgentCore fallback carrier (`assertion` in the `/invocations` envelope with a JCS body hash)
  is not implemented; the body hash is over the raw bytes received.
- The replay cache is per process and in memory: a restart empties it, and a callee with several
  replicas needs shared state (ADR open question 5). The 60 s registry refresh uses wall-clock time.
- Query handling: the target is canonicalized as the ADR lists. Literal characters in the query are
  kept as received (never re-encoded) so `+`, `%20` and `%2B` stay distinct; percent-escape hex is
  upper-cased and an escape of an unreserved character is rejected. An empty query (`/x?`) is signed
  as no query because ASGI does not distinguish it. A space or a `#` in the target is rejected.
- Plaintext HTTP on a private network is still an accepted interception risk (ADR-006, Transport).
- **Body-size cap (issue #101): covered in the application, proxy limits unverified.** The cap
  below refuses an over-limit request to a protected POST route (`/process`, `/invocations`,
  `/research`, `/research-and-assess`, `/assess`) with 413 before the assertion is checked, so an
  unauthenticated caller cannot make a service buffer more than the limit (plus one chunk). It
  applies only to those routes and only when authentication is on; `/health`, `/ping`, `/model` and
  the GET deck routes (no request body is expected) are not covered. What remains an assumption: whatever proxy sits in front of each runtime may buffer or
  cap bodies itself before the request reaches the app, and I have **not verified** the limits or
  buffering behavior of DigitalOcean App Platform, Amazon Bedrock AgentCore Runtime or any
  load balancer. Treat the in-app cap as the only guarantee and the proxy as unknown.

## Request body size cap

Code: `src/viharbot/body_limit.py` (pure ASGI middleware, installed by each service only when
`VIHARBOT_SERVICE_AUTH` is on; nothing changes when it is off).

- A request whose `Content-Length` exceeds the limit is answered `413` immediately, before the
  assertion check and without reading any of the body.
- Otherwise bytes are counted as the body streams (chunked or understated `Content-Length`); the
  moment the total crosses the limit the service answers `413` and stops reading. A body of exactly
  the limit passes.
- The 413 body is the generic `{"detail": "request body too large"}`. The log line records the
  agent, reason and limit only, never body, headers or token.
- Setting: `VIHARBOT_MAX_BODY_BYTES`, an integer from 1024 to 16777216 (invalid values stop startup).
  Default **262144 (256 KiB)**.

The same default applies to Research, Security, Sage and Pixel. Their legitimate requests are
smaller than the synthesis call (Research: a question; Security: one findings object; Sage and Pixel:
a question plus results already bounded as above), but the Security and Pixel bodies are not length
bounded by their schemas either, so raise the setting there if a real request is refused.

Default rationale: the largest legitimate specialist request is the synthesis call, which carries
the question (at most 6000 characters, so at most 24,000 bytes of UTF-8) plus the structured
results of the other agents in `context` (research findings, security, fact-check, source-quality,
privacy and Sage; the Sage analysis alone is bounded to roughly 40 KB by its field limits).
The research findings and other results are not length-bounded by their schemas, so this is an
estimate, not a proof; 256 KiB is several times the expected size and small enough to be cheap to
buffer. Raise it per service if a real request is refused.

Per runtime (same in-app limit everywhere; the proxy limits are unverified, see above):

| Runtime | In-app limit | Proxy in front |
| --- | --- | --- |
| Local Docker Compose (dev) | `VIHARBOT_MAX_BODY_BYTES`, default 256 KiB | none |
| DigitalOcean App Platform (Stage) | same | not verified |
| AgentCore Runtime | same, on `/invocations` | not verified |
| Fargate or other load balancer | same | not verified |
