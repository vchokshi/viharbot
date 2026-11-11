# Memory

Each agent service (research, security, fact-check, source-quality, privacy-review, synthesis and sage) writes an append-only JSONL file in its own memory directory:

- Research: `data/research/memory.jsonl`
- Security: `data/security/memory.jsonl`
- Fact Check: its private container memory directory
- Source Quality: its private container memory directory
- Privacy Review: its private container memory directory
- Synthesis: its private container memory directory
- Sage: its private container memory directory

Each record contains a timestamp, request ID, and that agent's structured result. **Nothing reads these files back.** `remember()` only appends (`src/viharbot/memory.py`), so memory is an audit trail, not context an agent can retrieve. Durable, retrievable, isolated memory is planned, not built. The memory helper does not intentionally save the incoming question, browser transcript, another agent's private history, or credentials. Generated results may still include sensitive information, so protect memory storage appropriately.

Local Compose uses separate bind-mounted directories for Research and Security and separate named volumes for the four specialist containers. DigitalOcean App Platform writes under `/tmp`; records there are ephemeral and may be lost when an instance is replaced. The web service keeps up to four prior chat turns in browser memory and sends them as context; it does not persist the transcript.

## Governed memory service (new path, not wired in)

A separate governed memory path now exists in code: the memory service (`src/viharbot/memory_service/`), backed by PostgreSQL, with versioned records, policy-table authorization, retire and erase, and audit ([memory service](memory-service.md), [ADR-009](adr/ADR-009-persistent-memory.md)). **Nothing calls it yet.** No agent reads or writes it, it is not deployed to Stage or Production, and it runs locally only under the opt-in Compose profile `memory`. `remember()` and its JSONL files are unchanged and stay non-authoritative traces: they are never read by the service, never imported into it, and must not be mistaken for governed records.
