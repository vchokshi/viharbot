-- Bind each idempotency entry to the derived owner (agent id) that created it (issue #40,
-- security finding on replay authorization). Additive and safe on an existing volume: one nullable
-- column, backfilled from the record (or its tombstone) that the entry refers to. The service
-- refuses a replay whose entry has no owner or a different owner, so an entry that cannot be
-- backfilled fails closed instead of being served. Existing grants already cover the new column.

ALTER TABLE memory.idempotency ADD COLUMN IF NOT EXISTS owner_agent_id text;

UPDATE memory.idempotency i
   SET owner_agent_id = r.owner_agent_id
  FROM memory.records r
 WHERE r.memory_id = i.memory_id AND i.owner_agent_id IS NULL;

UPDATE memory.idempotency i
   SET owner_agent_id = t.owner_agent_id
  FROM memory.tombstones t
 WHERE t.memory_id = i.memory_id AND i.owner_agent_id IS NULL;
