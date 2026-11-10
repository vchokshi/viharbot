# Deployment strategy

## Local development

`compose.yaml` runs the web chat and six model-backed agents on a private bridge network (`agent-network`). The web container receives the optional orchestration settings (`CHARLIE_PLANNER_MODEL`, `VIHARBOT_DEV_DIAGNOSTICS_ENABLED`) and, only if you opt in through `WEB_LITELLM_API_KEY`, a LiteLLM key for Charlie's planner. The browser reaches the web container at `http://127.0.0.1:8080`; agent ports are not published to the host. Research and Security use independent bind-mounted memory directories. Fact Check, Source Quality, Privacy Review, and Synthesis use individual named volumes. The optional orchestrator profile remains a CLI-style REST demonstration. Service authentication is on by default here; see below.

### Service authentication in local Compose

Local Compose runs with service authentication on by default (ADR-006 A2, [#235](https://github.com/vchokshi/viharbot/issues/235)): every agent service verifies a signed assertion on its non-health routes, and the web service, Research (to Security) and the `demo`-profile orchestrator sign their calls. Run `python scripts/dev_service_auth.py` once to generate development keys and a registry under `data/dev-auth/` (gitignored); Compose mounts them at run time, and no key or registry is in source or an image. Set `VIHARBOT_SERVICE_AUTH=off` and `VIHARBOT_OUTBOUND_AUTH=off` in `.env` to run without it. Details and limits: [service authentication](service-authentication.md).

**Decision (2026-11-10): Compose only, Stage unchanged.** `deploy/digitalocean/app.yaml` does not set either variable, so Stage verifies and signs nothing, and AgentCore is untouched. Stage key storage and distribution are open questions in ADR-006 (where the private keys live, how registries are distributed and rotated), and a Stage change needs the "Do you want to promote this dev build to Stage?" confirmation above.

**Trigger to enable it on Stage** (recorded for #235): before any non-private Stage use, that is any use with real prompts or client data, any public or shared exposure of the agent services, or an AgentCore runtime that other agents call. At that point decide Stage key storage, add the verifier settings and a public registry to the app spec, give each signing service (web, Research) its own key, run the offline suite, and ask the promotion question for that specific build. Until then Stage agent services trust the private network.

## Deployments after pushing to main

Pushing to `main` only updates the repository. It does not deploy to a provider. DigitalOcean App Platform, Amazon Bedrock AgentCore, and other targets are separate choices made after the code is pushed. Deployment commands and credentials stay provider-specific; there is no automatic deployment workflow.

## DigitalOcean App Platform

### Promotion approval

Local Docker Compose is dev; the DigitalOcean App Platform app is Stage. The App Spec sets `deploy_on_push: false` on all GitHub-backed components, so a push to `main` does not deploy them. Choose a DigitalOcean deployment explicitly when desired. Before deploying, finish local implementation and verification, summarize the exact change and any added monthly cost, then ask: **“Do you want to promote this dev build to Stage?”** Wait for an affirmative answer for that specific build. After deploying, verify the deployment phase, running components, and public health endpoint, then report the outcome.

`deploy/digitalocean/app.yaml` describes seven Dockerfile-backed components from the GitHub repository. Web serves the public browser interface on port 8080. All six agents are private services on internal port 8000. The four specialist agents share `Dockerfile.specialist` but each has a distinct component name, `AGENT_KIND`, model alias, and private memory path.

Configure encrypted runtime values for `LITELLM_API_KEY` on every model-using agent, plus `WEB_USERNAME` and `WEB_PASSWORD` on Web. By default keep the web component free of model credentials. **Exception:** iterative orchestration needs Charlie's planner to run in the web container, which requires a `LITELLM_API_KEY` secret and `CHARLIE_PLANNER_MODEL` there, which widens what a compromise of the public component exposes. The app spec enables Basic Auth on every web route except `/health`.

**What the committed spec deploys.** It sets neither `CHARLIE_PLANNER_MODEL` nor a LiteLLM key on the web component, so the live team (which has no flag and runs only when the planner is configured) is off and a Stage app created from it runs the fixed pipeline and `/api/chat/stream` answers `409`. Repository guardrail tests require that no orchestration flag exists in shipped configuration and that the Stage spec carries no planner alias or key (local Compose defaults the planner alias to `gpt-6-luna`, but the planner stays off), so enabling the live feature on Stage is a deliberate, separately approved change to the deployed app, not a repository default. Stage model aliases in the spec: `gpt-6.1-sol` (research; about 2.7 times the per-token price of `openai/gpt-5.4-mini` and slower, which matters for the request time budget), `gpt-5.4-mini` (privacy-review, synthesis; the gateway no longer routes the `openai/` prefixed name), `openai/gpt-5.4` (security), `gemini/gemini-3.5-flash` (fact-check, source-quality).

**Current state (2026-10-28).** No `viharbot-stage` app exists. One was created on 2026-10-27 from a locally modified copy of this spec, redeployed from `c646c42`, checked (all seven components running, `/health` returning 200, one authenticated live stream through the App Platform ingress delivering events incrementally), and then deleted at the owner's request. This is recorded here as history, not as a running deployment.

The App Platform web process listens on `0.0.0.0` and uses `PORT` (default 8080). Agent services listen on 8000. The agent memories use `/tmp` and are ephemeral. Review the number of billable components and model usage before operating the full six-agent pipeline.

**Slide-deck flag by environment.** `VIHARBOT_PPT_GENERATION_ENABLED` is **false on Stage**: the spec sets it explicitly and has no pixel component, so Stage never builds decks. It is **true in production** (AWS) once that deployment is configured; no production configuration exists in this repository yet, so nothing sets it today. Local Compose is true while Pixel is tested. See [Pixel slide service](pixel-slide-service.md).

No cloud resources are provisioned automatically.

## Other targets

The containers can run on a DigitalOcean Droplet with Docker Compose, with separate host firewall and TLS setup. The FastAPI services now include AgentCore's Runtime HTTP contract and can use port 8080 when built for `linux/arm64`. AWS identity configuration is still required before deployment, and no AWS resources or AgentCore runtimes exist. The AgentCore adapter exposes only `/api/chat` as `/invocations`; `POST /api/chat/stream` is not bridged. AgentCore Runtime is not static website hosting; the browser assets need a separate web hosting option, and a browser-facing authenticated API path must invoke the web coordinator. See the AgentCore deployment notes for the verified constraints and next steps.
