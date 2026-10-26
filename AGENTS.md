# Repository guidance

## Project intent

ViharBot implements a browser-based chat app and six independently deployed FastAPI agents: Research, Fact Check, Source Quality, Security, Privacy Review, and Synthesis. The web container coordinates them over private JSON REST. Keep proposals distinct from implemented features.

## Invariants

- All foundation model inference goes through the external LiteLLM project using its OpenAI-compatible API.
- Configure each model-using agent with `LITELLM_BASE_URL`, `LITELLM_API_KEY`, and its own model alias. Never deploy or modify LiteLLM, add provider-native inference, or put credentials in source.
- Do not send real keys or sensitive prompts over public HTTP. Permit credentials over HTTPS or HTTP to a literal loopback/private IP only (for secure local tunnels/private networking).
- Use plain JSON REST for agent communication. Do not add A2A, brokers, service meshes, or orchestration frameworks unless the user changes this direction.
- Keep each agent independently deployable with independent configuration and memory. Start with simple isolated storage; do not add databases without a concrete need.
- Begin with structured JSON logs, including agent, request ID, destination agent, model alias, duration, and errors. Avoid full prompts, secrets, and private memory in logs.
- Do not provision AWS resources unless explicitly requested. AgentCore capabilities must be verified before depending on them.

## Dev-to-Stage promotion gate

- Treat local Docker Compose as dev and DigitalOcean App Platform as Stage.
- Before any action that deploys or could trigger deployment from dev to Stage, finish the local implementation and verification, then ask the user explicitly: **“Do you want to promote this dev build to Stage?”** Include the commit or change summary, verification status, and any material cost change.
- Wait for an affirmative answer for that specific build. Do not infer approval from earlier deployments, general access to `doctl`, or prior authorization for other changes.
- Do not run `doctl apps update`, create a Stage deployment, or push to a branch that can trigger Stage deployment before the user approves. When in doubt, leave the completed work in dev and ask.
- After approval, report whether the deployment ran, what happened, and whether it succeeded. Verify DigitalOcean deployment status and the public health endpoint; approval alone does not establish success.

## Implementation style

Keep the learning example minimal and document limitations, especially local unauthenticated interfaces and placeholder behavior. Prefer standard library HTTP clients where practical. Do not add or run tests unless requested; never call a real inference endpoint with user credentials as an implicit check.
