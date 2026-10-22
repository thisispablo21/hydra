CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    cwd TEXT,
    model TEXT,
    started_at TEXT NOT NULL,
    last_event_at TEXT NOT NULL,
    last_tool TEXT,
    last_tool_input_summary TEXT,
    files_changed TEXT DEFAULT '[]',
    end_reason TEXT,
    archived_at TEXT,
    remote_control_url TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    tool_name TEXT,
    tool_input_summary TEXT,
    received_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_received ON events(received_at);

-- Server-distributed instructions and behavioural skills. Each document has
-- one common markdown body plus optional per-harness slot values; instructions
-- is the single reserved document rendered to CLAUDE.md / AGENTS.md.
CREATE TABLE IF NOT EXISTS skills (
    name                TEXT PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('instructions', 'skill')),
    enabled             INTEGER NOT NULL DEFAULT 1,
    implicit_invocation INTEGER NOT NULL DEFAULT 0,
    instances           TEXT,  -- NULL = every machine; else a JSON array of instance ids
    updated_at          TEXT NOT NULL,
    CHECK ((kind = 'instructions') = (name = 'instructions'))
);

CREATE TABLE IF NOT EXISTS skill_variants (
    name    TEXT NOT NULL REFERENCES skills(name) ON DELETE CASCADE,
    variant TEXT NOT NULL,  -- 'common' or a harness id
    body    TEXT NOT NULL,  -- markdown for common; a JSON slot map otherwise
    PRIMARY KEY (name, variant)
);

-- Exact append-only snapshots of the reserved instructions document.
CREATE TABLE IF NOT EXISTS document_versions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_name TEXT NOT NULL CHECK (document_name = 'instructions'),
    snapshot TEXT,
    published_at TEXT,
    recorded_at TEXT NOT NULL,
    source TEXT NOT NULL,
    author_instance_id TEXT,
    author_session_id TEXT
);
CREATE TRIGGER IF NOT EXISTS document_versions_no_update
BEFORE UPDATE ON document_versions BEGIN
    SELECT RAISE(ABORT, 'document versions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS document_versions_no_delete
BEFORE DELETE ON document_versions BEGIN
    SELECT RAISE(ABORT, 'document versions are append-only');
END;

CREATE TABLE IF NOT EXISTS memory_topics (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL
);

-- Server-distributed slash commands. One row per command; name is the
-- slash-command name without ".md" (e.g. "sync", "finish"). Content is an
-- opaque markdown blob - the server never interprets it. Clients pull these
-- into ~/.claude/commands/<name>.md.
CREATE TABLE IF NOT EXISTS config_commands (
    name       TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Server-distributed policy hooks. One row per hook, carrying BOTH the script
-- body and its settings.json wiring - they must never travel separately, because
-- `python <missing>.py` exits 2 and exit 2 on PreToolUse is the *blocking* code,
-- so wiring that outruns its script turns a fail-open guard into a hard deny on
-- every tool call. Clients install scripts and wiring in each harness's own
-- config directory.
CREATE TABLE IF NOT EXISTS config_hooks (
    name       TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    runtime    TEXT NOT NULL,          -- 'python' | 'bash': interpreter + file suffix
    event      TEXT NOT NULL,          -- Claude Code hook event, e.g. PreToolUse
    matcher    TEXT,                   -- NULL = emit no matcher key
    timeout    INTEGER NOT NULL,
    wiring     TEXT NOT NULL DEFAULT '{}', -- per-harness event/matcher/timeout JSON
    enabled    INTEGER NOT NULL DEFAULT 1,
    instances  TEXT,                   -- NULL = every machine; else a JSON array
                                       -- of HYDRA_INSTANCE_ID values
    updated_at TEXT NOT NULL
);

-- Project registry. Paths live in project_paths so the same project can
-- exist at different filesystem paths on different machines.
CREATE TABLE IF NOT EXISTS projects (
    slug TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Set when the project was created by the auto-register endpoint; cleared
    -- by the dashboard "Confirm" action. NULL means manually registered.
    auto_registered_at TEXT
);

-- One canonical path per (slug, instance_id). Re-registering the same slug
-- from a machine updates that row; a new machine adds a new row.
CREATE TABLE IF NOT EXISTS project_paths (
    slug TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    instance_id TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Set when this path was attached by auto-register; cleared by Confirm.
    auto_registered_at TEXT,
    PRIMARY KEY (slug, instance_id)
);

CREATE INDEX IF NOT EXISTS idx_project_paths_path ON project_paths(path);

-- Cross-machine memory store. project_slug NULL => global; otherwise pinned
-- to a project. Names are globally unique, scope-independent: one name = one
-- memory. Legacy DBs used two partial unique indexes instead, which let a
-- global row and a pinned row share a name - that is what let a stale mirror
-- file re-insert a deleted memory as a second row (the duplicate-memory bug).
--
-- The UNIQUE lives inline on the column rather than as a CREATE UNIQUE INDEX
-- below because db.get_db() runs this script BEFORE db._migrate(): a bare
-- CREATE UNIQUE INDEX would abort startup on a legacy DB that still holds
-- duplicates. Fresh DBs (and tests/conftest.py, which only runs this file) get
-- uniqueness from here; db._ensure_unique_memory_names() collapses duplicates
-- and installs idx_memories_name on pre-existing DBs.
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL CHECK (type IN ('user', 'feedback', 'project', 'reference')),
    body TEXT NOT NULL DEFAULT '',
    project_slug TEXT REFERENCES projects(slug) ON DELETE SET NULL,
    author_harness TEXT,
    author_session_id TEXT,
    author_model TEXT,
    topics TEXT, -- NULL = unclassified; JSON [] = catalog only
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Cross-machine token accounting. One row per API message, keyed on the
-- transcript's message.id.
--
-- message_id is the PRIMARY KEY and ingest is INSERT OR IGNORE: that is the
-- whole correctness story. Claude Code writes one assistant record per content
-- block, all repeating the same usage (480 records for 234 messages in one
-- measured session - 2.55x inflation if summed naively), and a resumed or
-- forked session copies prior history into a NEW transcript file, so the same
-- message.id legitimately arrives twice under two different session_ids. The
-- client's byte offsets only decide what is *sent*; they may be wrong in either
-- direction without corrupting the data.
--
-- session_id deliberately carries NO foreign key, unlike events.session_id:
-- `hydra usage backfill` imports transcripts for sessions that predate Hydra or
-- ran on a machine that never reported them, and with PRAGMA foreign_keys=ON a
-- FK would reject exactly those rows.
--
-- Cache writes are split 5m/1h because they price differently (1.25x vs 2x base
-- input). Claude Code writes 1h cache in practice, so collapsing the two would
-- misprice the write side by 60%. Cost itself is never stored - pricing happens
-- at query time (server/pricing.py) so a rate correction fixes all history.
CREATE TABLE IF NOT EXISTS usage_messages (
    message_id   TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    instance_id  TEXT NOT NULL,
    harness      TEXT NOT NULL DEFAULT 'claude-code',
    ts           TEXT NOT NULL,      -- record timestamp from the transcript
    cwd          TEXT,               -- resolved to a project at query time
    model        TEXT NOT NULL,
    effort       TEXT,               -- record-level `effort`; NULL on older models
    is_subagent  INTEGER NOT NULL DEFAULT 0,
    agent_type   TEXT,               -- `attributionAgent` ("Explore", ...)
    service_tier TEXT,
    speed        TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests   INTEGER NOT NULL DEFAULT 0,
    web_fetch_requests    INTEGER NOT NULL DEFAULT 0,
    received_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_messages(ts);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_messages(session_id);
