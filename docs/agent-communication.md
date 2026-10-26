# Agent communication

The web service coordinates agents using plain JSON REST. The same pattern is used inside Docker Compose and DigitalOcean App Platform's private app network.

## Flow

1. The web service accepts a browser message and generates or validates a request ID.
2. It sends `POST /research` to ResearchAgent with `X-Request-ID` and a JSON question.
3. ResearchAgent returns a `ResearchFindings` object.
4. The web service sends `POST /assess` to SecurityAgent with the same request ID and findings JSON.
5. SecurityAgent returns a `SecurityAssessment` object.
6. The web service returns both structures to the browser.

Research and Security echo the request ID in their response header and body. A missing or malformed ID is replaced with a UUID. The ID correlates logs; it is not authentication.

## Contracts

The browser posts `{ "message": "...", "history": [...] }` to `/api/chat`. History contains at most four user/assistant message snippets from the current tab. The web service formats those turns as contextual input for ResearchAgent. It does not save browser history. SecurityAgent receives the current request's validated `ResearchFindings`, not chat history.

ResearchAgent returns `{ request_id, agent, requested_model_alias, findings }`. SecurityAgent returns `{ request_id, agent, requested_model_alias, assessment }`. The browser-facing response is `{ request_id, findings, assessment }`. Pydantic schemas validate the agent payloads.

## Network and credentials

Locally, the web container is published only at `127.0.0.1:8080`; agent ports are private to Compose. On DigitalOcean, the web service is the only component with a public HTTP port. Research and Security expose internal port 8000 to components in the same app.

The web-to-agent messages use HTTP within the private app/Compose network. They contain user input and research output, so keep these services private. LiteLLM API keys are sent only from the model-using agent services to the HTTPS LiteLLM endpoint. The web container does not receive those keys.
