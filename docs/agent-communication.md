# Agent communication

The web service coordinates six private FastAPI agents with plain JSON REST. Every call carries the same `X-Request-ID`; agent responses echo the ID in their body and response header. The ID correlates logs and is not authentication.

## Flow

1. The browser posts `{ "message": "...", "history": [...] }` to `/api/chat`. The browser keeps at most four previous turns in tab memory.
2. The web service sends the question and request ID to ResearchAgent at `POST /research`.
3. It sends the structured findings to Fact Check and Source Quality at `POST /process`.
4. It sends the same findings to SecurityAgent at `POST /assess`.
5. It sends the current question and structured team results to Privacy Review at `POST /process`.
6. It sends the question and all structured results to Synthesis at `POST /process`.
7. The web service returns the typed findings, four specialist outputs, and synthesis to the browser.

Fact Check only compares supplied claims with supplied evidence. Source Quality only inspects provided references; it does not fetch them. Both report these limits. The coordinator currently calls the services sequentially and logs each destination, duration, and error type without logging prompts or results.

## Contracts

- ResearchAgent accepts `{ "question": "..." }` and returns a `ResearchFindings` structure.
- Fact Check and Source Quality accept `{ "findings": ResearchFindings }` and return their respective typed result in a `result` field.
- SecurityAgent accepts `{ "findings": ResearchFindings }` and returns `SecurityAssessment`.
- Privacy Review and Synthesis accept `{ "question": "...", "context": { ... } }` and return their respective typed result in a `result` field.
- The browser-facing response contains `request_id`, `findings`, `fact_check`, `source_quality`, `assessment`, `privacy_review`, and `synthesis`.

Pydantic validates all public and agent result structures. A malformed incoming request ID is replaced with a UUID. Model output failures return a generic error and do not disclose prompts or provider response bodies.

## Network and credentials

Locally, only the web container is published at `127.0.0.1:8080`; agent ports are private to the Compose network. On DigitalOcean, only the web service has a public HTTP port. Each agent has a distinct internal component name and private port 8000.

The web service and agents exchange questions and research results over private-network HTTP. Model-using agents send LiteLLM API keys only to the configured LiteLLM endpoint over HTTPS. The web container does not receive those keys. Privacy Review also sends the question and structured results to LiteLLM for analysis.
