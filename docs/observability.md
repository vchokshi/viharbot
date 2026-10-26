# Observability

Each service emits structured JSON logs. Records include agent, request ID, destination agent when relevant, model alias, duration, and error type. The web service passes the same request ID to ResearchAgent and SecurityAgent, so a single browser request can be followed across containers.

Prompts, chat history, findings, API keys, and passwords are not included in application logs. OpenTelemetry and hosted trace collection are deferred. DigitalOcean App Platform provides component logs; no log export destination is configured by this repository.
