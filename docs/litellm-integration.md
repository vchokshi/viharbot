# LiteLLM integration

LiteLLM is an external project already running at https://litellm.viharchokshi.com. ViharBot does not deploy or modify it.

## Environment

- LITELLM_BASE_URL: defaults to the supplied HTTPS endpoint.
- LITELLM_API_KEY: proxy key injected into each model-using agent container.
- RESEARCH_MODEL: openai/gpt-5.4-mini (user reports a successful HTTP 200 chat completion).
- SECURITY_MODEL: openai/gpt-5.4 (candidate alias; must pass a real inference check).

Aliases are passed to Strands LiteLLMModel with use_litellm_proxy enabled. The API base URL is normalized to end in /v1. No provider model IDs or provider credentials are configured here.

## Connectivity and inference

scripts/check_litellm.py calls the OpenAI-compatible GET /v1/models endpoint without generating text. Tests mock this call by default. The opt-in connectivity test makes this non-inference request when RUN_LITELLM_INTEGRATION=1 is set. Catalog presence does not verify model usability. The live SecurityAgent inference test uses openai/gpt-5.4 and requires an HTTP 200 structured result. Run it before using SecurityAgent in the demo. It is gated by RUN_LITELLM_INFERENCE=1; leave it off for the default mock-only suite.

Run the standalone connectivity check inside the test image with: docker compose --profile tests run --build --rm tests python scripts/check_litellm.py

Agent inference uses Strands structured output on LiteLLM's OpenAI-compatible chat completions API. ResearchAgent uses RESEARCH_MODEL; SecurityAgent uses SECURITY_MODEL. Errors fail the HTTP request safely; there is no provider fallback.

Keys are sent in Authorization headers by the SDK and never logged or passed to the other agent. Keep .env out of version control and rotate proxy credentials through the LiteLLM operator.
