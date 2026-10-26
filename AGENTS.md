# Repository guidance

## Project intent

ViharBot Phase 1 implements ResearchAgent and SecurityAgent as independently deployable FastAPI services; the long-term design has six planned agents. Prefer simple HTTP/HTTPS REST with JSON and keep proposals distinct from implemented agents.

## Invariants

- All foundation model inference goes through the external LiteLLM project using its OpenAI-compatible API.
- Configure the consumer with `LITELLM_BASE_URL`, `LITELLM_API_KEY`, and per-agent `RESEARCH_MODEL` / `SECURITY_MODEL` aliases. Never deploy or modify LiteLLM, add provider-native inference, or put credentials in source.
- Do not send real keys or sensitive prompts over public HTTP. Permit credentials over HTTPS or HTTP to a literal loopback/private IP only (for secure local tunnels/private networking).
- Use plain JSON REST for agent communication. Do not add A2A, brokers, service meshes, or orchestration frameworks unless the user changes this direction.
- Keep each agent independently deployable with independent configuration and memory. Start with simple isolated storage; do not add databases without a concrete need.
- Begin with structured JSON logs, including agent, request ID, destination agent, model alias, duration, and errors. Avoid full prompts, secrets, and private memory in logs.
- Do not provision AWS resources unless explicitly requested. AgentCore capabilities must be verified before depending on them.

## Implementation style

Keep the learning example minimal and document limitations, especially local unauthenticated interfaces and placeholder behavior. Prefer standard library HTTP clients where practical. Do not add or run tests unless requested; never call a real inference endpoint with user credentials as an implicit check.
