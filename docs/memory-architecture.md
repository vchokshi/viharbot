# Memory

Each agent writes to an independent JSONL file:

- ResearchAgent: data/research/memory.jsonl
- SecurityAgent: data/security/memory.jsonl

Compose mounts only the corresponding agent directory into each container. Each record stores request ID, timestamp, and that agent's structured result. It does not store the incoming prompt or another agent's private history. This is simple local experimentation storage, not production persistence or access control.
