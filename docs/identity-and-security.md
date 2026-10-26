# Identity and security

## Web access

Local Compose binds the web app to loopback and disables Basic Auth by default for learning. Do not widen the local bind address without enabling access control. The DigitalOcean app spec enables HTTP Basic Auth for `/` and `/api/chat`; set `WEB_USERNAME` and `WEB_PASSWORD` as encrypted runtime secrets before handling real requests. `/health` remains open for platform health checks.

This is one shared password pair, not per-user identity, authorization, rate limiting, or audit attribution. Add proper user authentication and request limits before using the service with multiple people or sensitive prompts.

## Agent and model boundaries

DigitalOcean exposes only the web service publicly. Research and Security use internal ports. Locally, their ports are not published. Agent-to-agent calls are not authenticated within either private network, so do not attach untrusted containers or expose their ports.

`LITELLM_API_KEY` is configured on the Research and Security services only. It is sent to the LiteLLM endpoint over HTTPS, never to the web service, and never logged. The model aliases are separate service settings. Per-agent cloud identities and independent permissions are not yet implemented.

Chat messages travel from the browser to the web service and then to ResearchAgent, and the current findings travel to SecurityAgent. ResearchAgent treats prior chat turns as untrusted context. SecurityAgent treats research findings as untrusted data rather than instructions. Model output is advisory and should be reviewed for consequential decisions.

The app is unauthenticated if `WEB_AUTH_ENABLED` is false. Protect public deployments, set secrets outside source control, and do not log prompts, findings, API keys, or passwords.
