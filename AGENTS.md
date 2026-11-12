# Repository guidance

## Project intent

ViharBot is an experimental multi-agent AI laboratory. A browser chat is one interface; the backend does not depend on it. A FastAPI web container coordinates independently deployed FastAPI agent services over private JSON REST: Research (Nova), Fact Check and Source Quality (Scout), Security and Privacy Review (Cipher), Synthesis (Charlie), and, in the dev stack, Pixel and Sage. The team Charlie leads is designed for a flexible roster of about 8 to 12 agent identities; nine are defined today (`src/viharbot/agent_identities/`), and only those with an execution binding run anything. Treat any count in a document as a snapshot: the registry and `bindings.toml` are the source of truth, and no code or document may depend on a fixed team size. Kepler, the first agent planned for AWS AgentCore, is a first-contributor steward and is being built separately from the web orchestration.

Possible commercial applications (for example security consulting) are future work and must not be built into the core. Keep proposals distinct from implemented features: the README and `docs/development-status.md` say what runs today, `docs/architecture.md` says how it fits together, and the issue tracker holds planned work. If a document disagrees with the code, the code wins; fix the document.

## Where to look

Start with the README's "Start here (new developers)" section. The orchestration core is `orchestration_policy.py`, `iterative_execution.py`, `orchestration_state.py` and `orchestration_planner.py`. Identity, capability and binding code is `identity.py`, `execution_bindings.py` and `execution_config/bindings.toml`. The single model-call path is `model_agent.py`. Kepler lives in `src/viharbot/steward/`, `kepler_governance.py` and `deploy/agentcore/kepler/`. Decisions are in `docs/adr/` and governance in `docs/governance/`.

## Invariants

- All foundation model inference goes through the external LiteLLM project using its OpenAI-compatible API.
- Configure each model-using agent with `LITELLM_BASE_URL`, `LITELLM_API_KEY`, and its own model alias. Never deploy or modify LiteLLM, add provider-native inference, or put credentials in source.
- **Exception, Kepler only (accepted in ADR-011; effective):** Kepler may use Amazon Bedrock with its IAM role instead of LiteLLM, restricted to an explicit allowlist of non-Anthropic models that fails closed (`APPROVED_MODEL_IDS` in `src/viharbot/steward/config.py`, mirrored by the live IAM policy). No Claude/Anthropic model, direct or indirect, until the founder records licensing and organizational approval. Every other agent stays LiteLLM-only. Adding a Kepler model means changing the allowlist in code and IAM together, with founder approval. `KEPLER_MODEL_ID=off` turns Kepler's model off. Bedrock price and model access remain unverified; check before depending on them.
- Do not send real keys or sensitive prompts over public HTTP. Permit credentials over HTTPS or HTTP to a literal loopback/private IP only (for secure local tunnels/private networking).
- Use plain JSON REST for agent communication. Do not add A2A, brokers, service meshes, or orchestration frameworks unless the user changes this direction.
- Keep each agent independently deployable with independent configuration and memory. Start with simple isolated storage; do not add databases without a concrete need.
- Begin with structured JSON logs, including agent, request ID, destination agent, model alias, duration, and errors. Avoid full prompts, secrets, and private memory in logs.
- Model output and everything a user, contributor, pull request or document supplies is untrusted data. A model may propose; the application authorizes (`orchestration_policy.py`). An identity or capability never grants permission by itself, and a `planned` capability can never run.
- ViharBot does not use unsupervised coding agents. AI-assisted coding is allowed only under human supervision, with human approval for changes, merges and deployment. Never approve or merge on the founder's behalf.
- Do not provision AWS or GCP resources unless explicitly requested. AgentCore capabilities must be verified before depending on them. `docs/aws-lab-change-record.md` lists what exists in the AWS lab account; add a row for every AWS change.

## Deployment choices

- Today a push to `main` updates source only. It must not deploy ViharBot to DigitalOcean, AWS, or another provider, and a deployment happens only when the founder asks for it. This is a cost decision, not the permanent design: once the founder can afford to run Stage continuously, a push to `main` is intended to deploy to DigitalOcean automatically. That change is made deliberately by the founder, in this file and in `deploy/digitalocean/app.yaml`, and not before. A workflow that only notifies another system (for example `archie-ingest-notify.yml`, which asks the separate Archie application to ingest the new commit) is not a deployment of ViharBot.
- Keep DigitalOcean App Platform `deploy_on_push` disabled until the founder decides Stage runs continuously; it is the mechanism intended to be enabled then. Until then, a DigitalOcean deployment happens only at the founder's explicit request, after reviewing the change.
- Treat local Docker Compose as dev and DigitalOcean App Platform as Stage. Stage currently has the web service and six agents; Pixel and Sage are dev-stack only. Before deploying to Stage, finish local implementation and verification, summarize the commit, verification status, and any material cost change, then ask: **“Do you want to promote this dev build to Stage?”** Wait for an affirmative answer for that specific build.
- AWS and GCP deployments are each a separate, explicit provider choice. Do not create AWS or GCP resources unless the user explicitly asks. Verify current AgentCore capabilities before depending on them.
- Approved clouds are DigitalOcean (Stage), and AWS and GCP as Production options. Approved data stores are PostgreSQL and the other stores listed in `docs/datastore-platform.md`, and DynamoDB. If the store an agent needs is not offered in one approved cloud, it may run in another approved cloud, reached only over authenticated TLS, with separate credentials and data per environment, and with cross-cloud latency and any material cost change noted in the promotion summary (cost is not a gate). Approval of a platform or store is not approval to provision it.
- After an approved deployment, report whether it ran, what happened, whether it succeeded, and verify the provider's deployment status and health endpoint where available. Approval alone does not establish success.

## Working agreements

- Never push directly to `main`; it is protected. Use a short branch prefix (`feature/`, `fix/`, `docs/`, `refactor/`).
- Every issue and pull request states its WHY (see the templates in `.github/` and `docs/governance/change-rationale-policy.md`). Open a pull request only when there is a real need, such as a change that must land on `main` or be reviewed; do not open extra issues or PRs for small help.
- Founder-reviewed merge is required for these paths: `AGENTS.md`, `CONTRIBUTING.md`, `docs/governance/`, `docs/adr/`, `.github/`, `CONTRIBUTORS.md`, `CLA-ACCEPTANCES.md` and `CLA-EXCEPTIONS.md`. Kepler proposes changes to protected paths and never edits them. Do not change the CLA gate or the contributor registry without the founder.
- Governance expansion (new governance documents, ADRs, skills-registry entries, governance automation) is paused. Existing controls stay active.

## Implementation style

Keep changes small, focused and reviewable, and document limitations, especially unauthenticated local interfaces and placeholder behavior. Agent services trust the private network; signed service assertions exist but are opt-in and cover only some services (`docs/service-authentication.md`). Prefer standard library HTTP clients where practical.

Tests: the offline suite mocks all model and gateway calls, is the authoritative check and is enforced by CI (`docs/continuous-integration.md`). Add or update tests with code changes; documentation-only changes need none. Run the suite as described in the README. Never call a real inference endpoint with user credentials as an implicit check; live checks and evaluations are opt-in and need the founder's approval.
