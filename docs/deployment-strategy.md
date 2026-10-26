# Deployment strategy

## Local development

`compose.yaml` runs a web chat container and the two agent containers on a private bridge network. The browser reaches the web container at `http://127.0.0.1:8080`; ResearchAgent and SecurityAgent have no host-published ports. The optional orchestrator profile remains for a CLI-style REST demonstration. Local agent memories persist in separate bind-mounted directories.

## DigitalOcean App Platform

`deploy/digitalocean/app.yaml` describes three Dockerfile-backed components from the GitHub repository. The web component serves the public browser interface on port 8080. Research and Security are private services on internal port 8000. The web service uses their component names as REST destinations.

Before creating the app, configure encrypted runtime values for `LITELLM_API_KEY` separately on Research and Security, plus `WEB_USERNAME` and `WEB_PASSWORD` on Web. The app spec enables Basic Auth on the web UI and chat endpoint. It leaves each secret value empty as a placeholder. The `/health` endpoint does not require login.

The App Platform web process listens on `0.0.0.0` and uses `PORT` (default 8080). Private agent services listen on 8000. DigitalOcean App Platform supports Dockerfile source components and private internal ports; see the [App Spec reference](https://docs.digitalocean.com/products/app-platform/reference/app-spec/) and [internal routing guide](https://docs.digitalocean.com/products/app-platform/how-to/manage-internal-routing/).

No cloud resources are provisioned automatically. Review region, instance sizes, secrets, and repository access before running `doctl apps create --spec deploy/digitalocean/app.yaml`. The services do not use persistent storage in the current App Platform spec; their `/tmp` memory files can be lost on replacement.

## Other targets

The containers remain independently buildable and can run on a DigitalOcean Droplet with Docker Compose, but that requires separate host firewall and TLS setup. Amazon Bedrock AgentCore is not configured or assumed compatible with this FastAPI deployment.
