# Observability

Agents emit structured JSON application logs. Each record includes agent name, request ID, requested model alias, elapsed milliseconds, and error type on failure. The orchestrator also logs the destination agent for each REST call.

Prompts, findings, API keys, and exception messages are not logged. The request ID is forwarded in X-Request-ID through the Research-to-Security flow. OpenTelemetry is deferred for Phase 1.
