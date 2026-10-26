# Memory

Each of the six agents writes an append-only JSONL file in its own memory directory:

- Research: `data/research/memory.jsonl`
- Security: `data/security/memory.jsonl`
- Fact Check: its private container memory directory
- Source Quality: its private container memory directory
- Privacy Review: its private container memory directory
- Synthesis: its private container memory directory

Each record contains a timestamp, request ID, and that agent's structured result. The memory helper does not intentionally save the incoming question, browser transcript, another agent's private history, or credentials. Generated results may still include sensitive information, so protect memory storage appropriately.

Local Compose uses separate bind-mounted directories for Research and Security and separate named volumes for the four specialist containers. DigitalOcean App Platform writes under `/tmp`; records there are ephemeral and may be lost when an instance is replaced. The web service keeps up to four prior chat turns in browser memory and sends them as context; it does not persist the transcript.
