import json
from datetime import UTC, datetime

import aiosqlite

from server.config import BASE_DIR, DB_PATH

_db: aiosqlite.Connection | None = None


def _utcnow() -> str:
    """Matches routers/memory._now() - microseconds included, because
    memories.updated_at is a version token and two writes must never collide."""
    return datetime.now(UTC).isoformat()


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        # Initialize schema
        schema_path = BASE_DIR / "schema.sql"
        await _db.executescript(schema_path.read_text())
        await _migrate(_db)
        await _db.commit()
    return _db


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Idempotent in-place migrations for existing DBs.

    schema.sql uses CREATE TABLE IF NOT EXISTS, so new columns on pre-existing
    tables don't appear without an ALTER. Keep this short - each block should
    check for its target state before acting.
    """
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'claude_md'"
    )
    if await cursor.fetchone():
        has_instructions = await conn.execute(
            "SELECT 1 FROM skills WHERE name = 'instructions'"
        )
        if not await has_instructions.fetchone():
            legacy = await conn.execute("SELECT content FROM claude_md WHERE id = 1")
            row = await legacy.fetchone()
            if row is not None:
                now = _utcnow()
                await conn.execute(
                    """INSERT INTO skills
                           (name, kind, enabled, implicit_invocation, instances, updated_at)
                       VALUES ('instructions', 'instructions', 1, 0, NULL, ?)""",
                    (now,),
                )
                await conn.execute(
                    """INSERT INTO skill_variants (name, variant, body)
                       VALUES ('instructions', 'common', ?)""",
                    (row[0],),
                )
        await conn.execute("DROP TABLE claude_md")

    cursor = await conn.execute("PRAGMA table_info(memories)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "project_slug" not in cols:
        await conn.execute(
            "ALTER TABLE memories ADD COLUMN project_slug TEXT "
            "REFERENCES projects(slug) ON DELETE SET NULL"
        )
    if "author_harness" not in cols:
        await conn.execute("ALTER TABLE memories ADD COLUMN author_harness TEXT")
        await conn.execute(
            "UPDATE memories SET author_harness = 'claude-code'"
        )
    if "author_session_id" not in cols:
        await conn.execute("ALTER TABLE memories ADD COLUMN author_session_id TEXT")
    if "topics" not in cols:
        await conn.execute("ALTER TABLE memories ADD COLUMN topics TEXT")
    if "author_model" not in cols:
        await conn.execute("ALTER TABLE memories ADD COLUMN author_model TEXT")

    from server.services.instruction_history import seed_instructions

    await seed_instructions(conn)
    await _ensure_unique_memory_names(conn)

    cursor = await conn.execute("PRAGMA table_info(sessions)")
    session_cols = {row[1] for row in await cursor.fetchall()}
    if "archived_at" not in session_cols:
        await conn.execute("ALTER TABLE sessions ADD COLUMN archived_at TEXT")
    if "remote_control_url" not in session_cols:
        await conn.execute("ALTER TABLE sessions ADD COLUMN remote_control_url TEXT")
    # Partial index references archived_at, so it lives here (after the ALTER)
    # rather than in schema.sql - schema.sql runs before this migration.
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_active "
        "ON sessions(last_event_at) WHERE archived_at IS NULL"
    )

    # projects.path → project_paths(slug, instance_id='legacy', path)
    cursor = await conn.execute("PRAGMA table_info(projects)")
    proj_cols = {row[1] for row in await cursor.fetchall()}
    if "path" in proj_cols:
        await conn.execute(
            "INSERT OR IGNORE INTO project_paths "
            "(slug, instance_id, path, created_at, updated_at) "
            "SELECT slug, 'legacy', path, created_at, updated_at FROM projects"
        )
        await conn.execute("ALTER TABLE projects DROP COLUMN path")

    # auto_registered_at columns (added when /api/projects/auto-register landed)
    cursor = await conn.execute("PRAGMA table_info(projects)")
    proj_cols = {row[1] for row in await cursor.fetchall()}
    if "auto_registered_at" not in proj_cols:
        await conn.execute("ALTER TABLE projects ADD COLUMN auto_registered_at TEXT")
    cursor = await conn.execute("PRAGMA table_info(project_paths)")
    pp_cols = {row[1] for row in await cursor.fetchall()}
    if "auto_registered_at" not in pp_cols:
        await conn.execute(
            "ALTER TABLE project_paths ADD COLUMN auto_registered_at TEXT"
        )

    cursor = await conn.execute("PRAGMA table_info(usage_messages)")
    usage_cols = {row[1] for row in await cursor.fetchall()}
    if "harness" not in usage_cols:
        await conn.execute(
            "ALTER TABLE usage_messages ADD COLUMN harness TEXT NOT NULL"
            " DEFAULT 'claude-code'"
        )

    cursor = await conn.execute("PRAGMA table_info(config_hooks)")
    hook_cols = {row[1] for row in await cursor.fetchall()}
    if "wiring" not in hook_cols:
        await conn.execute(
            "ALTER TABLE config_hooks ADD COLUMN wiring TEXT NOT NULL DEFAULT '{}'"
        )
        rows = await conn.execute_fetchall(
            "SELECT name, event, matcher, timeout FROM config_hooks"
        )
        for row in rows:
            wiring = {
                "claude-code": {
                    "event": row[1],
                    "matcher": row[2],
                    "timeout": row[3],
                }
            }
            await conn.execute(
                "UPDATE config_hooks SET wiring = ? WHERE name = ?",
                (json.dumps(wiring), row[0]),
            )


async def _has_unique_name_index(conn: aiosqlite.Connection) -> bool:
    """True if a full (non-partial) UNIQUE index over exactly (name) exists.

    Accepts both physical shapes: sqlite_autoindex_memories_1, created by
    schema.sql's inline UNIQUE on a fresh DB, and idx_memories_name, created by
    _ensure_unique_memory_names on a migrated one.
    """
    cursor = await conn.execute("PRAGMA index_list(memories)")
    for row in await cursor.fetchall():
        # (seq, name, unique, origin, partial)
        if not row[2] or row[4]:
            continue
        cur = await conn.execute(f'PRAGMA index_info("{row[1]}")')
        if [r[2] for r in await cur.fetchall()] == ["name"]:
            return True
    return False


async def _ensure_unique_memory_names(conn: aiosqlite.Connection) -> None:
    """Enforce globally-unique memory names on a pre-existing DB.

    Legacy DBs indexed (name) WHERE global and (name, project_slug) WHERE pinned,
    so a global row and a pinned row could share a name. That is the shape the
    duplicate-memory bug lived in: deleting a memory server-side left its mirror
    file behind, and the next Stop-hook push re-inserted it under the other scope
    as a second row, which the partial indexes happily allowed.

    Idempotent: a no-op once a full UNIQUE(name) index is in place.
    """
    if await _has_unique_name_index(conn):
        return

    # 1. Exact twins: a global row byte-identical to a project-pinned one is a
    #    stale-mirror re-insert of a memory someone deliberately pinned. The
    #    pinned row - the scope a human chose - survives. Content-matched, so
    #    this is lossless by construction. Logged: this is the only destructive
    #    step in the migration, so it must never run silently.
    twin_sql = (
        " FROM memories WHERE project_slug IS NULL AND EXISTS ("
        " SELECT 1 FROM memories p WHERE p.project_slug IS NOT NULL"
        " AND p.name = memories.name AND p.body = memories.body"
        " AND p.description = memories.description)"
    )
    for row in await conn.execute_fetchall("SELECT id, name" + twin_sql):
        print(
            f"hydra: migration dropped resurrected global memory #{row[0]} {row[1]!r}"
            " (identical to the project-pinned row of the same name)"
        )
    await conn.execute("DELETE" + twin_sql)

    # 2. Any name still duplicated has divergent content (or came from another
    #    machine's DB), so we must NOT guess which one to drop. Rename the
    #    losers instead - never delete. Lowest id keeps the name.
    rows = list(await conn.execute_fetchall(
        "SELECT id, name, project_slug FROM memories ORDER BY id"
    ))
    taken = {r[1] for r in rows}
    seen: set[str] = set()
    for mem_id, name, slug in ((r[0], r[1], r[2]) for r in rows):
        if name not in seen:
            seen.add(name)
            continue
        base = f"{name}-{slug}" if slug else f"{name}-global"
        candidate = base
        suffix = 0
        while candidate in taken:
            # Must strictly advance every iteration - an earlier version cycled
            # between two fixed candidates and hung startup forever.
            suffix += 1
            candidate = f"{base}-{mem_id}" if suffix == 1 else f"{base}-{mem_id}-{suffix}"
        # Bump updated_at: it is the version token sync compares against, and a
        # rename the mirror files have not seen must look like a change to them.
        await conn.execute(
            "UPDATE memories SET name = ?, updated_at = ? WHERE id = ?",
            (candidate, _utcnow(), mem_id),
        )
        taken.add(candidate)
        seen.add(candidate)
        print(f"hydra: migration renamed duplicate memory #{mem_id} {name!r} -> {candidate!r}")

    # 3. Swap the partial indexes for one full UNIQUE index. If this still
    #    raises, let it propagate out of get_db() and refuse to start - a
    #    half-migrated DB that boots is worse than one that doesn't.
    await conn.execute("DROP INDEX IF EXISTS idx_memories_global")
    await conn.execute("DROP INDEX IF EXISTS idx_memories_project")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_name ON memories(name)"
    )


async def close_db():
    global _db
    if _db is not None:
        await _db.close()
        _db = None
