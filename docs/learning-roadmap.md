# Learning roadmap

> **Historical (Phase 1 era).** This roadmap predates identities, orchestration and live streaming. For current status see [development status](development-status.md).

1. Run the browser chat locally and inspect the six-agent JSON flow using one request ID.
2. Compare research findings, supplied-evidence Fact Check results, Source Quality limitations, Security assessment, Privacy recommendations, and final Synthesis.
3. Inspect separate memory locations and verify logs contain agent identities and destinations without prompts or private results.
4. Test prompt injection in browser history and in structured findings; review how each agent is instructed to treat other agents' output as untrusted data.
5. Review the Stage app spec, encrypted model keys, public web authentication, and ephemeral memory tradeoffs.
6. Add real user authentication, rate limits, durable sessions, and independent agent permissions when the learning goals require them.

Fact Check and Source Quality review only supplied content and do not browse. No AWS resources or AgentCore runtime are configured. A native client may be considered later; the current application is web-based.
