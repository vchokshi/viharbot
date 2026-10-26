# Identity and security

Phase 1 keeps both agent APIs on a private Docker bridge network and publishes no ports. The LiteLLM API key is supplied through LITELLM_API_KEY, sent only to the HTTPS proxy, and never logged or passed to the other agent. Keep local .env files private.

Agent-to-agent REST calls are not authenticated within this initial Compose network. Do not attach untrusted containers or publish these services. Add authentication and TLS before deploying agents across hosts.

Research findings are untrusted data. SecurityAgent's system prompt tells the model to analyze them as data, not follow instructions embedded in them. Model responses are advisory and should be reviewed for consequential decisions.
