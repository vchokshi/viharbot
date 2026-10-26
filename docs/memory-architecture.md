# Memory

ResearchAgent and SecurityAgent write to separate append-only JSONL files:

- ResearchAgent: `data/research/memory.jsonl`
- SecurityAgent: `data/security/memory.jsonl`

Each record stores a request ID, timestamp, and that agent's structured result. It does not store the incoming question, chat transcript, another agent's private history, or secrets. Compose mounts each agent directory separately.

On DigitalOcean App Platform the template writes under `/tmp/viharbot-memory`; that storage is ephemeral and may be lost when an instance is replaced. The web service holds up to four prior chat turns in browser memory and sends them as context for the next research request. It does not persist the transcript. This is a learning setup, not durable or production-grade memory.
