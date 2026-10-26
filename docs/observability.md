# Observability

Each of the six agents and the web coordinator emits structured JSON logs. Records include agent identity, request ID, destination agent when relevant, model alias, duration, and error type. The web service forwards one request ID across the full pipeline so a browser request can be traced across all containers.

Prompts, chat history, findings, API keys, and passwords are excluded from application logs. OpenTelemetry and hosted trace collection are deferred. DigitalOcean App Platform provides component logs; no export destination is configured by this repository.
