# Deployment strategy

## Local development

`compose.yaml` runs the web chat and six model-backed agents on a private bridge network. The browser reaches the web container at `http://127.0.0.1:8080`; agent ports are not published to the host. Research and Security use independent bind-mounted memory directories. Fact Check, Source Quality, Privacy Review, and Synthesis use individual named volumes. The optional orchestrator profile remains a CLI-style REST demonstration.

## Deployments after pushing to main

Pushing to `main` only updates the repository. It does not deploy to a provider. DigitalOcean App Platform, Amazon Bedrock AgentCore, and other targets are separate choices made after the code is pushed. Deployment commands and credentials stay provider-specific; there is no automatic deployment workflow.

## DigitalOcean App Platform

### Promotion approval

Local Docker Compose is dev; the DigitalOcean App Platform app is Stage. The App Spec sets `deploy_on_push: false` on all GitHub-backed components, so a push to `main` does not deploy them. Choose a DigitalOcean deployment explicitly when desired. Before deploying, finish local implementation and verification, summarize the exact change and any added monthly cost, then ask: **“Do you want to promote this dev build to Stage?”** Wait for an affirmative answer for that specific build. After deploying, verify the deployment phase, running components, and public health endpoint, then report the outcome.

`deploy/digitalocean/app.yaml` describes seven Dockerfile-backed components from the GitHub repository. Web serves the public browser interface on port 8080. All six agents are private services on internal port 8000. The four specialist agents share `Dockerfile.specialist` but each has a distinct component name, `AGENT_KIND`, model alias, and private memory path.

Configure encrypted runtime values for `LITELLM_API_KEY` on every model-using agent, plus `WEB_USERNAME` and `WEB_PASSWORD` on Web. Keep the web component free of model credentials. The app spec enables Basic Auth on the web UI and chat endpoint; `/health` does not require login.

The App Platform web process listens on `0.0.0.0` and uses `PORT` (default 8080). Agent services listen on 8000. The agent memories use `/tmp` and are ephemeral. Review the number of billable components and model usage before operating the full six-agent pipeline.

No cloud resources are provisioned automatically.

## Other targets

The containers can run on a DigitalOcean Droplet with Docker Compose, with separate host firewall and TLS setup. The FastAPI services now include AgentCore's Runtime HTTP contract and can use port 8080 when built for `linux/arm64`. AWS identity configuration is still required before deployment. AgentCore Runtime is not static website hosting; the browser assets need a separate web hosting option, and a browser-facing authenticated API path must invoke the web coordinator. See the AgentCore deployment notes for the verified constraints and next steps.
