"""Migration of a legacy memories table to globally-unique names.

Legacy DBs enforced uniqueness with two PARTIAL indexes - (name) WHERE global and
(name, project_slug) WHERE pinned - so one name could exist twice, once global and
once pinned. That is the shape the duplicate-memory bug lived in: a delete or a
re-scope left the mirror file behind, and the next Stop-hook push re-inserted the
old identity as a second row under the other scope.

These tests build that legacy shape by hand and run the real _migrate().
"""

import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest

from server import db as db_module
from server.services.skills import render

pytestmark = pytest.mark.asyncio

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

LEGACY_MEMORIES = """
DROP TABLE IF EXISTS memories;
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL CHECK (type IN ('user', 'feedback', 'project', 'reference')),
    body TEXT NOT NULL DEFAULT '',
    project_slug TEXT REFERENCES projects(slug) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_memories_global
    ON memories(name) WHERE project_slug IS NULL;
CREATE UNIQUE INDEX idx_memories_project
    ON memories(name, project_slug) WHERE project_slug IS NOT NULL;
"""

LEGACY_CLAUDE_MD = """
CREATE TABLE claude_md (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    content TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


async def _legacy_db(tmp_path: Path) -> aiosqlite.Connection:
    """A DB whose memories table still has the two partial unique indexes."""
    conn = await aiosqlite.connect(tmp_path / "legacy.db")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_PATH.read_text())
    await conn.executescript(LEGACY_MEMORIES)
    await conn.execute(
        "INSERT INTO projects (slug, description, created_at, updated_at)"
        " VALUES ('pquant', '', 't', 't')"
    )
    await conn.commit()
    return conn


async def _insert(conn, name, *, slug=None, body="b", desc="d", mem_type="project"):
    cur = await conn.execute(
        "INSERT INTO memories (name, description, type, body, project_slug,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 't', 't')",
        (name, desc, mem_type, body, slug),
    )
    return cur.lastrowid


async def _names(conn) -> list[tuple]:
    rows = await conn.execute_fetchall(
        "SELECT id, name, project_slug FROM memories ORDER BY id"
    )
    return [(r["id"], r["name"], r["project_slug"]) for r in rows]


async def test_migration_collapses_exact_twin_keeping_pinned(tmp_path: Path):
    """The real-world case: /forget re-scoped a memory (pinned row, lower id) and
    the Stop-hook push resurrected the deleted global from a stale mirror file
    (global row, higher id). Bodies are byte-identical, so the pinned row - the
    scope a human chose - wins and the resurrection is dropped."""
    conn = await _legacy_db(tmp_path)
    pinned = await _insert(conn, "htmx", slug="pquant", body="SAME", desc="SAME")
    ghost = await _insert(
        conn, "htmx", slug=None, body="SAME", desc="SAME", mem_type="feedback",
    )
    await conn.commit()

    await db_module._migrate(conn)
    await conn.commit()

    assert await _names(conn) == [(pinned, "htmx", "pquant")]
    assert ghost not in [r[0] for r in await _names(conn)]
    assert await db_module._has_unique_name_index(conn)
    await conn.close()


async def test_migration_renames_divergent_duplicate_never_deletes(tmp_path: Path):
    """A same-name pair whose CONTENT differs is not a known twin - we cannot
    guess which is authoritative, so the loser is renamed, never deleted."""
    conn = await _legacy_db(tmp_path)
    keeper = await _insert(conn, "dup", slug="pquant", body="ONE")
    loser = await _insert(
        conn, "dup", slug=None, body="TWO", mem_type="feedback",
    )
    await conn.commit()

    await db_module._migrate(conn)
    await conn.commit()

    rows = await _names(conn)
    assert len(rows) == 2  # nothing lost
    assert (keeper, "dup", "pquant") in rows  # lowest id keeps the name
    renamed = next(r for r in rows if r[0] == loser)
    assert renamed[1] == "dup-global"
    assert await db_module._has_unique_name_index(conn)
    await conn.close()


async def test_migration_rename_terminates_when_fallbacks_are_taken(tmp_path: Path):
    """The rename loop must strictly advance. An earlier version cycled between
    two fixed candidates and hung get_db() forever - the server never started."""
    conn = await _legacy_db(tmp_path)
    keeper = await _insert(conn, "dup", slug="pquant", body="ONE")
    loser = await _insert(conn, "dup", slug=None, body="TWO", mem_type="feedback")
    # Occupy every fallback name the loser would reach for.
    await _insert(conn, "dup-global", slug=None, mem_type="user")
    await _insert(conn, f"dup-global-{loser}", slug=None, mem_type="user")
    await _insert(conn, f"dup-global-{loser}-1", slug=None, mem_type="user")
    await conn.commit()

    await asyncio.wait_for(db_module._migrate(conn), timeout=10)
    await conn.commit()

    rows = await _names(conn)
    assert len(rows) == 5  # nothing lost
    assert (keeper, "dup", "pquant") in rows
    renamed = next(r for r in rows if r[0] == loser)
    assert renamed[1] not in ("dup", "dup-global", f"dup-global-{loser}",
                              f"dup-global-{loser}-1")
    assert await db_module._has_unique_name_index(conn)
    await conn.close()


async def test_migration_bumps_updated_at_on_rename(tmp_path: Path):
    """updated_at is the version token sync compares against, so a rename the
    mirror files have not seen must look like a change to them."""
    conn = await _legacy_db(tmp_path)
    await _insert(conn, "dup", slug="pquant", body="ONE")
    loser = await _insert(conn, "dup", slug=None, body="TWO", mem_type="feedback")
    await conn.commit()

    await db_module._migrate(conn)
    await conn.commit()

    rows = list(await conn.execute_fetchall(
        "SELECT updated_at FROM memories WHERE id = ?", (loser,)
    ))
    assert rows[0]["updated_at"] != "t"
    await conn.close()


async def test_migration_is_idempotent(tmp_path: Path):
    conn = await _legacy_db(tmp_path)
    await _insert(conn, "a", slug="pquant")
    await _insert(conn, "a", slug=None, mem_type="feedback", body="other")
    await conn.commit()

    await db_module._migrate(conn)
    await conn.commit()
    first = await _names(conn)

    await db_module._migrate(conn)  # second boot
    await conn.commit()
    assert await _names(conn) == first
    await conn.close()


async def test_migration_backfills_authorship_only_once(tmp_path: Path):
    conn = await _legacy_db(tmp_path)
    memory_id = await _insert(
        conn, "legacy", slug=None, mem_type="feedback"
    )
    await conn.commit()

    await db_module._migrate(conn)
    await conn.commit()
    row = next(iter(await conn.execute_fetchall(
        "SELECT author_harness, author_session_id, author_model"
        " FROM memories WHERE id = ?",
        (memory_id,),
    )))
    assert tuple(row) == ("claude-code", None, None)

    await conn.execute(
        "UPDATE memories SET author_harness = NULL WHERE id = ?", (memory_id,)
    )
    await db_module._migrate(conn)
    await conn.commit()
    row = next(iter(await conn.execute_fetchall(
        "SELECT author_harness FROM memories WHERE id = ?", (memory_id,)
    )))
    assert row["author_harness"] is None
    await conn.close()


async def test_migration_rejects_duplicate_after_migrating(tmp_path: Path):
    """After the swap, the DB itself refuses a second row with the same name."""
    conn = await _legacy_db(tmp_path)
    await _insert(conn, "solo", slug="pquant")
    await conn.commit()
    await db_module._migrate(conn)
    await conn.commit()

    with pytest.raises(Exception, match="UNIQUE"):
        await _insert(conn, "solo", slug=None, mem_type="feedback")
    await conn.close()


async def test_fresh_schema_has_unique_name_index(tmp_path: Path):
    """A fresh DB gets uniqueness from schema.sql's inline UNIQUE, so _migrate
    finds nothing to do (tests/conftest.py only runs schema.sql)."""
    conn = await aiosqlite.connect(tmp_path / "fresh.db")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_PATH.read_text())
    assert await db_module._has_unique_name_index(conn)
    await conn.close()


async def test_claude_md_migrates_verbatim_then_table_is_dropped(tmp_path: Path):
    conn = await aiosqlite.connect(tmp_path / "claude-md.db")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_PATH.read_text())
    await conn.executescript(LEGACY_CLAUDE_MD)
    content = "Keep this literal: {{x}}\n"
    await conn.execute(
        "INSERT INTO claude_md (id, content, updated_at) VALUES (1, ?, 'old')",
        (content,),
    )
    await db_module._migrate(conn)
    await conn.commit()

    row = next(iter(await conn.execute_fetchall(
        """SELECT s.kind, v.variant, v.body
           FROM skills s JOIN skill_variants v ON v.name = s.name
           WHERE s.name = 'instructions'"""
    )))
    assert tuple(row) == ("instructions", "common", content)
    assert render(row[2], None) == content
    assert not await conn.execute_fetchall(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'claude_md'"
    )

    await conn.execute(
        "UPDATE skill_variants SET body = 'edited' WHERE name = 'instructions'"
    )
    await db_module._migrate(conn)
    rows = list(await conn.execute_fetchall(
        "SELECT body FROM skill_variants WHERE name = 'instructions'"
    ))
    assert rows[0][0] == "edited"
    await conn.close()


async def test_empty_legacy_claude_md_creates_no_instructions(tmp_path: Path):
    conn = await aiosqlite.connect(tmp_path / "empty-claude-md.db")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_PATH.read_text())
    await conn.executescript(LEGACY_CLAUDE_MD)
    await db_module._migrate(conn)
    assert not await conn.execute_fetchall(
        "SELECT 1 FROM skills WHERE name = 'instructions'"
    )
    assert not await conn.execute_fetchall(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'claude_md'"
    )
    await conn.close()


async def test_migration_adds_usage_harness_with_default(tmp_path: Path):
    conn = await aiosqlite.connect(tmp_path / "legacy-usage.db")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_PATH.read_text())
    await conn.execute("DROP TABLE usage_messages")
    await conn.execute(
        "CREATE TABLE usage_messages (message_id TEXT PRIMARY KEY, model TEXT)"
    )
    await conn.execute(
        "INSERT INTO usage_messages (message_id, model) VALUES ('old', 'model')"
    )

    await db_module._migrate(conn)
    await conn.commit()
    row = next(iter(await conn.execute_fetchall(
        "SELECT harness FROM usage_messages WHERE message_id = 'old'"
    )))
    assert row["harness"] == "claude-code"

    info = await conn.execute_fetchall("PRAGMA table_info(usage_messages)")
    harness = next(row for row in info if row["name"] == "harness")
    assert harness["notnull"] == 1
    assert harness["dflt_value"] == "'claude-code'"
    await conn.close()


async def test_migration_backfills_hook_wiring_from_legacy_columns(tmp_path: Path):
    conn = await aiosqlite.connect(tmp_path / "legacy-hooks.db")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_PATH.read_text())
    await conn.execute("DROP TABLE config_hooks")
    await conn.execute(
        """CREATE TABLE config_hooks (
               name TEXT PRIMARY KEY, content TEXT NOT NULL, runtime TEXT NOT NULL,
               event TEXT NOT NULL, matcher TEXT, timeout INTEGER NOT NULL,
               enabled INTEGER NOT NULL DEFAULT 1, instances TEXT, updated_at TEXT NOT NULL
           )"""
    )
    await conn.execute(
        """INSERT INTO config_hooks
               (name, content, runtime, event, matcher, timeout, updated_at)
           VALUES ('guard', 'body', 'python', 'PreToolUse', 'Bash', 17, 't')"""
    )

    await db_module._migrate(conn)
    await conn.commit()
    row = next(iter(await conn.execute_fetchall(
        "SELECT wiring FROM config_hooks WHERE name = 'guard'"
    )))
    assert json.loads(row["wiring"]) == {
        "claude-code": {"event": "PreToolUse", "matcher": "Bash", "timeout": 17}
    }

    await db_module._migrate(conn)
    again = next(iter(await conn.execute_fetchall(
        "SELECT wiring FROM config_hooks WHERE name = 'guard'"
    )))
    assert again["wiring"] == row["wiring"]
    await conn.close()


async def test_migration_topics_are_null_and_remain_authored(tmp_path: Path):
    conn = await _legacy_db(tmp_path)
    memory_id = await _insert(conn, "legacy", slug=None, mem_type="user", body="literal\n")
    await conn.commit()
    await db_module._migrate(conn)
    await conn.commit()
    row = next(iter(await conn.execute_fetchall(
        "SELECT id, name, body, topics FROM memories WHERE id = ?", (memory_id,)
    )))
    assert tuple(row) == (memory_id, "legacy", "literal\n", None)
    await conn.execute("UPDATE memories SET topics = '[]' WHERE id = ?", (memory_id,))
    await db_module._migrate(conn)
    row = next(iter(await conn.execute_fetchall(
        "SELECT topics FROM memories WHERE id = ?", (memory_id,)
    )))
    assert row[0] == "[]"
    await conn.close()
