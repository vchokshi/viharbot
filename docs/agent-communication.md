# Agent communication

Phase 1 agents use HTTP REST with JSON over the private Docker Compose bridge network. Agent APIs have no published host ports.

## Flow

1. The orchestrator demo container generates a request ID.
2. It sends POST http://research:8000/research with X-Request-ID and a JSON question.
3. ResearchAgent returns a ResearchFindings object.
4. The orchestrator sends POST http://security:8000/assess with the same request ID and findings JSON.
5. SecurityAgent returns a SecurityAssessment object.

Both agents echo X-Request-ID in the response header and body. If the header is missing or malformed, an agent creates a UUID. The request ID is for correlation, not authentication.

## Contracts

Research input is a JSON object with a question string. Security input is the ResearchAgent response's findings object. Pydantic schemas validate both requests and both structured model responses.

The demo runs inside Docker and uses Compose DNS names. Services are independently built and configured. No A2A, broker, or orchestration framework is used.

## Security boundary

The Docker network is private to this Compose project and ports are not published. LiteLLM is accessed separately over HTTPS for inference. No proxy key is passed between agents. This local network is not a replacement for authentication when agents are later deployed on separate hosts.
