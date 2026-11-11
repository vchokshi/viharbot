-- Memory service schema, version 1 (ADR-009, issue #40, PR 1).
-- Run only by the migration role. The service role gets the minimum table privileges at the end
-- and holds no DDL rights. Plain SQL: no extensions, no triggers, no row-level security (RLS is
-- defense in depth in the Gate 0 proposal and is not part of this change).

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memory_service') THEN
        RAISE EXCEPTION 'role memory_service must exist before this migration runs';
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS memory;

-- One row per logical record: the authoritative current policy and state (classification,
-- retention, lifecycle, rev). Content lives in record_versions. Authorization decisions are made
-- from this row only, never from an older version.
CREATE TABLE memory.records (
    memory_id        uuid PRIMARY KEY,                  -- service-generated, never reused
    memory_type      text NOT NULL CHECK (memory_type IN ('agent_private', 'session')),
    scope_key        text NOT NULL,                     -- the agent id or the session id
    owner_agent_id   text NOT NULL,                     -- derived from the verified token only
    classification   text NOT NULL
                     CHECK (classification IN ('public', 'internal', 'confidential', 'restricted')),
    lifecycle        text NOT NULL DEFAULT 'active'
                     CHECK (lifecycle IN ('active', 'retired', 'erased')),
    rev              bigint NOT NULL CHECK (rev >= 1),  -- advances on EVERY mutation
    version          integer NOT NULL CHECK (version >= 1),  -- counts content versions only
    tags             text[] NOT NULL DEFAULT '{}',
    personal_data    boolean NOT NULL DEFAULT false,
    consent_basis    text NOT NULL DEFAULT 'not_applicable'
                     CHECK (consent_basis IN ('not_applicable', 'consent_recorded')),
    consent_reference text,
    retention_until  timestamptz,                       -- agent-private records
    expires_at       timestamptz,                       -- session records
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL,
    retired_at       timestamptz,
    erased_at        timestamptz,
    CHECK (memory_type <> 'session' OR expires_at IS NOT NULL),
    CHECK (memory_type <> 'agent_private' OR expires_at IS NULL),
    CHECK (memory_type <> 'agent_private' OR scope_key = owner_agent_id),
    CHECK (consent_basis <> 'consent_recorded' OR consent_reference IS NOT NULL)
);

CREATE INDEX records_scope_idx
    ON memory.records (owner_agent_id, memory_type, scope_key, lifecycle, created_at DESC);

-- Immutable content versions. Provenance is per version (who or what produced this text).
CREATE TABLE memory.record_versions (
    memory_id        uuid NOT NULL REFERENCES memory.records (memory_id),
    version          integer NOT NULL,
    content          text NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 65536),
    prov_kind        text NOT NULL,
    prov_source_id   text NOT NULL,
    prov_author      text NOT NULL,                     -- derived: the caller's agent id
    prov_request_id  text,
    created_at       timestamptz NOT NULL,
    PRIMARY KEY (memory_id, version)
);

-- Idempotent writes. The key space is per principal and scope, which is a superset of the ADR's
-- (principal, record, key) uniqueness: a create has no record id yet, so the key must be unique
-- before one exists. The stored result holds metadata only, never content. After an erase the
-- rows of that record are scrubbed (hash and result cleared, erased = true) and stay as key
-- tombstones, so a retried create cannot resurrect an erased record.
CREATE TABLE memory.idempotency (
    principal        text NOT NULL,
    scope_type       text NOT NULL,
    scope_key        text NOT NULL,
    idempotency_key  text NOT NULL,
    operation        text NOT NULL,
    request_hash     text NOT NULL,
    memory_id        uuid,
    status_code      integer,
    result           jsonb,
    erased           boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL,
    PRIMARY KEY (principal, scope_type, scope_key, idempotency_key)
);

CREATE INDEX idempotency_memory_idx ON memory.idempotency (memory_id) WHERE memory_id IS NOT NULL;

-- Stage one of an erase (the write barrier) leaves a pending row; stage two completes it.
CREATE TABLE memory.erase_requests (
    memory_id        uuid PRIMARY KEY,
    owner_agent_id   text NOT NULL,
    memory_type      text NOT NULL,
    scope_key        text NOT NULL,
    reason           text NOT NULL,
    state            text NOT NULL CHECK (state IN ('pending', 'physically_deleted')),
    requested_at     timestamptz NOT NULL,
    completed_at     timestamptz,
    request_id       text
);

-- Never deleted. Holds identifiers and timestamps only, no content.
CREATE TABLE memory.tombstones (
    memory_id        uuid PRIMARY KEY,
    owner_agent_id   text NOT NULL,
    memory_type      text NOT NULL,
    scope_key        text NOT NULL,
    erased_at        timestamptz NOT NULL,
    request_id       text
);

-- Least privilege for the one service role. No TRUNCATE, no DDL, no access to anything else.
REVOKE ALL ON SCHEMA memory FROM PUBLIC;
GRANT USAGE ON SCHEMA memory TO memory_service;
GRANT SELECT, INSERT, UPDATE, DELETE ON memory.records TO memory_service;
GRANT SELECT, INSERT, DELETE ON memory.record_versions TO memory_service;
GRANT SELECT, INSERT, UPDATE ON memory.idempotency TO memory_service;
GRANT SELECT, INSERT, UPDATE ON memory.erase_requests TO memory_service;
GRANT SELECT, INSERT ON memory.tombstones TO memory_service;
