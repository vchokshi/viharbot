# Learning roadmap

1. Run the browser chat locally and inspect the Research-to-Security JSON flow.
2. Compare structured findings, uncertainty, request IDs, and isolated agent memory files.
3. Inspect how chat context is limited to the previous four browser turns and how it is kept out of logs.
4. Test ResearchAgent's handling of prompt injection inside prior chat content and SecurityAgent's handling of untrusted findings.
5. Review the DigitalOcean App Spec, internal service routing, encrypted secrets, public web auth, and ephemeral memory tradeoffs before deploying.
6. Add real user authentication, rate limits, durable session storage, and per-agent permissions only when the learning goals require them.
7. Expand toward the long-term six-agent design when a concrete specialty warrants another service. Keep proposals distinct from implemented services.

The current client is a web app. A native or phone-specific client may be considered later. No AWS or DigitalOcean resources are provisioned by this repository.
