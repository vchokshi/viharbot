# Deployment strategy

Phase 1 runs ResearchAgent, SecurityAgent, and the short-lived orchestrator demo in Docker Compose. All services use Python 3.12 images and share a private bridge network. No service ports are published to the host. Agent containers can make outbound HTTPS requests to the separately managed LiteLLM proxy.

SecurityAgent's future Amazon Bedrock AgentCore target is not configured. Before moving it, confirm runtime packaging, network access to https://litellm.viharchokshi.com, environment secret injection, and compatibility with its FastAPI interface. No AWS resources are provisioned.

See the repository README for local demo and test commands. The optional connectivity test calls only the HTTPS /v1/models endpoint; inference tests are mocked.
