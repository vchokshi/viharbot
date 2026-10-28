# Identity and security

## Web access

Local Compose binds the web app to loopback and disables Basic Auth by default for learning. The DigitalOcean app spec enables HTTP Basic Auth for every web route except `/health` (`/`, `/images/*`, `/api/chat` and `/api/chat/stream`); configure `WEB_USERNAME` and `WEB_PASSWORD` as encrypted runtime secrets before handling real requests. `/health` remains open for platform health checks.

This is one shared password pair, not per-user identity, authorization, rate limiting, or audit attribution. Add proper user authentication and request limits before using the service with multiple people or sensitive prompts.

## Agent and model boundaries

DigitalOcean exposes only the web service publicly. All six agents use internal ports. Locally, their ports are not published. Agent-to-agent calls are not authenticated within the private network, so do not attach untrusted containers or expose agent ports.

Each agent has a separate container, configured model alias, and memory path. LiteLLM API keys are injected only into the six model-using services, sent to the configured HTTPS LiteLLM endpoint, and never logged. The web container receives a key only through the opt-in `WEB_LITELLM_API_KEY`, which iterative orchestration needs for Charlie's planner; that widens what a compromise of the public component exposes. Per-agent cloud identities and independent permissions are not implemented.

The web service sends questions to ResearchAgent. Findings are reviewed by Fact Check, Source Quality, and Security. Privacy Review receives the current question and structured team results, and Synthesis receives the question and all reviews. Fact Check compares only supplied evidence; Source Quality does not fetch references. These agents do not independently verify live facts. Privacy Review's model call sends the question and supplied results to LiteLLM, so users should avoid submitting information they are not permitted to share with that service.

ResearchAgent treats chat context as untrusted input. Specialist agents treat supplied findings and reviews as data, not instructions. Model output is advisory and should be reviewed for consequential decisions. The six-step pipeline consumes more model calls and has more latency than the original two-agent flow.
