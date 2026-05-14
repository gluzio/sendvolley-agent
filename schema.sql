-- SendVolley agent SQLite schema (v1). See ARCHITECTURE.md §7.
-- Idempotent: every CREATE uses IF NOT EXISTS. Re-running is safe.
-- No migration system in v1; schema changes will introduce one when needed.

CREATE TABLE IF NOT EXISTS clients (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    anthropic_api_key  TEXT,                       -- nullable; populated only when ANTHROPIC_KEY_MODE='client' (v2)
    created_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id            TEXT NOT NULL REFERENCES clients(id),
    role                 TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content              TEXT NOT NULL,
    twilio_message_sid   TEXT,
    from_number          TEXT,                     -- only set for inbound ('user') rows
    turn_id              TEXT,                     -- set on inbound; links to llm_calls/tool_calls
    created_at           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_client_created
    ON conversations(client_id, created_at DESC, id DESC);  -- id DESC is the same-ms tiebreaker (§11.4)

CREATE TABLE IF NOT EXISTS llm_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      TEXT NOT NULL REFERENCES clients(id),
    turn_id        TEXT NOT NULL,
    model          TEXT NOT NULL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    stop_reason    TEXT,
    latency_ms     INTEGER NOT NULL,
    error          TEXT,
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_turn ON llm_calls(turn_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    TEXT NOT NULL REFERENCES clients(id),
    turn_id      TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    tool_input   TEXT NOT NULL,                    -- JSON
    tool_result  TEXT,                             -- JSON or plain text
    is_error     INTEGER NOT NULL DEFAULT 0,       -- 0 / 1
    latency_ms   INTEGER NOT NULL,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_turn ON tool_calls(turn_id);

-- §11.6 — lightly categorized free-form prose; never hard-deleted.
CREATE TABLE IF NOT EXISTS memory_facts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      TEXT NOT NULL REFERENCES clients(id),
    category       TEXT NOT NULL,
    fact           TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    superseded_by  INTEGER REFERENCES memory_facts(id)
);
CREATE INDEX IF NOT EXISTS idx_memory_facts_client_category
    ON memory_facts(client_id, category)
    WHERE superseded_by IS NULL;

-- Expected `kind` values: 'worker_prompt_edit', 'system_prompt_edit', 'skill_addition', 'other'.
-- Not enforced via CHECK constraint (premature); documentation only.
CREATE TABLE IF NOT EXISTS proposals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      TEXT NOT NULL REFERENCES clients(id),
    kind           TEXT NOT NULL,
    summary        TEXT NOT NULL,
    body_markdown  TEXT NOT NULL,
    reviewed_at    INTEGER,
    created_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_failures (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ip         TEXT NOT NULL,
    reason            TEXT NOT NULL,
    headers_redacted  TEXT,                        -- JSON; secrets stripped before insert
    created_at        INTEGER NOT NULL
);
