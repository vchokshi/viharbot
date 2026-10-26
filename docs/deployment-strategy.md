# Deployment strategy

## Local development

`compose.yaml` runs the web chat and six model-backed agents on a private bridge network. The browser reaches the web container at `http://127.0.0.1:8080`; agent ports are not published to the host. Research and Security use independent bind-mounted memory directories. Fact Check, Source Quality, Privacy Review, and Synthesis use individual named volumes. The optional orchestrator profile remains a CLI-style REST demonstration.

## DigitalOcean App Platform

### Promotion approval

Local Docker Compose is dev; the DigitalOcean App Platform app is Stage. Finish the local implementation and verification, summarize the exact change and any added monthly cost, then ask the user explicitly whether to promote that dev build to Stage. Wait for an affirmative answer before running `doctl apps update`, creating a deployment, or pushing to a Stage-connected branch. Approval is specific to the reviewed build. After deploying, verify the deployment phase, running components, and public health endpoint, then report the outcome.

`deploy/digitalocean/app.yaml` describes seven Dockerfile-backed components from the GitHub repository. Web serves the public browser interface on port 8080. All six agents are private services on internal port 8000. The four specialist agents share `Dockerfile.specialist` but each has a distinct component name, `AGENT_KIND`, model alias, and private memory path.

Configure encrypted runtime values for `LITELLM_API_KEY` on every model-using agent, plus `WEB_USERNAME` and `WEB_PASSWORD` on Web. Keep the web component free of model credentials. The app spec enables Basic Auth on the web UI and chat endpoint; `/health` does not require login.

The App Platform web process listens on `0.0.0.0` and uses `PORT` (default 8080). Agent services listen on 8000. The agent memories use `/tmp` and are ephemeral. Review the number of billable components and model usage before operating the full six-agent pipeline.

No cloud resources are provisioned automatically. Amazon Bedrock AgentCore is not configured or assumed compatible with this FastAPI deployment.

## Other targets

The containers can run on a DigitalOcean Droplet with Docker Compose, with separate host firewall and TLS setup. AgentCore or other targets require their own deployment and identity compatibility work.
