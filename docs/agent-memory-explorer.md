# Agent Memory Explorer (phase 1)

A read-only, founder-only window into an agent's application-managed state: structured working
memory and decisions, never model reasoning. Phase 1 serves it from the existing web app at
`/agents/{agent_id}`; there is no new container, database or framework. Command Central (Founder
Mission Control, #301 and #305) stays the place for administration; both are meant to consume the
same governed contracts.

## What exists today

| Piece | State |
| --- | --- |
| Page shell `/agents/{id}`, agent selector from the identity registry | implemented |
| Charlie working-memory projection (`working_memory.py`) | implemented, tested |
| Portrait entry point and "Explore Charlie's Memory" link on Meet the team | implemented |
| Command Central link | configurable; shows "planned" until `COMMAND_CENTRAL_URL` is set |
| Persistent memory records (`charlie.synthesis.meta/1`) | **not connected** (phase 2) |
| Founder event stream | **not connected** (contract gap, below) |
| Other agents' working or durable memory | **not connected**; no agent is activated to fill the UI |
| Episodic, semantic, procedural, relationship memory | **not implemented**; shown as such, never populated |

## Data flow

`run_iterative_pipeline` registers its live `OrchestrationState` with an in-process registry.
The founder API projects that state on read. Nothing is written to disk or a database, at most 16
active and 5 finished runs are referenced, and a restart releases them. Registration is a no-op
unless `MEMORY_EXPLORER_ENABLED` is true, so a deployment without the explorer retains no question
text beyond the request. With several web workers each process sees only its own runs; history
before the process started is reported as unavailable, not empty.

## Projection contract (`viharbot.working-memory/1`)

Two tiers, both explicit allowlists built from typed fields (`ExecutionBudget`, `StepResult`,
`DecisionRecord`); raw state is never serialized.

* Metadata: request id, lifecycle (`active`, `completed`, `released`), status, iteration, decision
  count, completion kind and reason, elapsed time, rejected-action count, pending-gap count,
  peer-review count, budget and remaining budget, per-step identity, capability, service, version,
  duration, `based_on` provenance and whether the result is still current (False means superseded),
  and decision records with reason codes. No user or model text.
* Content (separate permission): the question, current goal, pending gaps, and per-step summary and
  evidence gaps. All of it is untrusted data and the page renders it with `textContent` only.

Not projected: prompts, envelopes, model aliases, URLs, credentials, hidden reasoning.

## API

All under `/api/memory-explorer`, all `Cache-Control: no-store`.

* `GET /status` public, content-free (`enabled` or `unavailable`).
* `GET /config`, `/agents`, `/agents/{id}`, `/agents/{id}/working-memory`,
  `/agents/{id}/persistent-memory`, `/agents/{id}/events` require the founder gate.
* Unknown or malformed ids return 404. Any agent other than Charlie gets `not_connected` and an
  empty list for working memory.

## Authorization

Deny by default and enforced server-side. Disabled returns 503; no or wrong credentials return 401;
a token shorter than 24 characters is treated as unconfigured (503). Two bearer tokens, compared in
constant time: `MEMORY_EXPLORER_FOUNDER_TOKEN` grants metadata, `MEMORY_EXPLORER_CONTENT_TOKEN`
grants content too. This is an interim local and test convention. Use it over HTTPS or loopback
only. It does not reuse any agent or workload identity. The production design is shared IAM, SSO
and resource-scoped authorization (#338, #343, #344), which is **not verified ready** and replaces
this gate. The page shell also sits behind the existing site auth when `WEB_AUTH_ENABLED` is on. The
token is held in page memory only, never in storage or a URL. Audit events log a fixed code (denied,
or read of metadata or content) and no content; richer IAM audit events belong to #347.

## Configuration

| Variable | Meaning |
| --- | --- |
| `MEMORY_EXPLORER_ENABLED` | switch, default off |
| `MEMORY_EXPLORER_FOUNDER_TOKEN`, `MEMORY_EXPLORER_CONTENT_TOKEN` | interim gate, 24+ characters |
| `MEMORY_EXPLORER_BASE_URL` | optional https (or localhost http) base for the portrait link, for example `https://agent-memory.viharchokshi.com`; default is relative |
| `COMMAND_CENTRAL_URL` | optional, same rules; unset means "planned" |
| `MEMORY_EXPLORER_ENVIRONMENT` | `local` (default), `stage` or `production`, display only |

Reverse navigation: Command Central can link to `{base}/agents/{agent_id}`. No DNS, Stage or
Production change is made by this work.

## Local development

```
MEMORY_EXPLORER_ENABLED=true \
MEMORY_EXPLORER_FOUNDER_TOKEN=local-founder-token-123456 \
MEMORY_EXPLORER_CONTENT_TOKEN=local-content-token-1234567 \
python -m uvicorn viharbot.web_app:app --port 8000
```

Open `http://localhost:8000/agents/charlie` and enter a token. Run a chat to see a live run.
Tests: `pytest tests/test_memory_explorer.py` (offline, no model calls).

## Known limitations and contract gaps

* Working memory exists only for requests this process handled; the planner can finish without
  calling synthesis, so not every request creates a persistent memory operation, and Charlie's
  synthesis memory and planner state are different structures shown separately.
* No founder event stream. The existing SSE stream is request-scoped and public-safe. A founder
  stream needs an additive, versioned, authorized contract (#348). Memory read, retrieval and denial
  events do not exist yet. The decision and step lists in the projection are the real, ordered
  record available today.
* No runtime telemetry, model alias, token or cost data (#47, #231, #348, #349).
* Desktop and mobile layout and keyboard use were checked by static tests only; see the PR for the
  manual and screenshot status.

## Epics advanced and blocked

Advances #24, #26 (Meet the team entry), #40 (read path design only), #47, #301 and #305 (link and
contract), #348 (projection). Blocked on shared IAM and sessions: #338, #343, #344, #347. Out of
scope: memory mutation, correction or deletion (#303, #305), relationships (#300, #302).

## Next phases

2. Founder-scoped read contract on the memory service for `/memory/agent/charlie/records`, with
   pagination, lifecycle and access audit. 3. Other agents as their contracts land. 4. Command Central.
