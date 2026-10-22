import json
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from server.auth import require_auth
from server.db import get_db
from server.models import HookUpsert, MemoryTopic
from server.routers.memory import require_flow
from server.services.instruction_history import append_revision, instant, now
from server.services.memory_routing import MEMORY_WRITE_LOCK
from server.services.skills import SKILLS_WRITE_LOCK, render, validate

router = APIRouter(prefix="/api/config", tags=["config"], dependencies=[Depends(require_auth)])

# A command or hook name maps 1:1 to a filename (<name>.md -> /<name>, or
# <name>.py) on the client, so restrict it to a filesystem- and command-safe
# charset: no path separators, no leading dot, nothing that could escape
# ~/.claude/commands or ~/.claude/hooks, or rename a command.
_CONFIG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@router.get("/claude-md/history")
async def instructions_history():
    async with SKILLS_WRITE_LOCK:
        db = await get_db()
        rows = await db.execute_fetchall(
            """SELECT revision_id, published_at, recorded_at, source,
                      author_instance_id, author_session_id
               FROM document_versions WHERE document_name = 'instructions'
               ORDER BY COALESCE(published_at, recorded_at) DESC, revision_id DESC"""
        )
    return [dict(row) for row in rows]


@router.get("/claude-md")
async def get_claude_md(at: str | None = None, harness: str | None = None):
    boundary = None
    if at is not None:
        try:
            boundary = instant(at)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    async with SKILLS_WRITE_LOCK:
        db = await get_db()
        if boundary is not None:
            rows = list(
                await db.execute_fetchall(
                    """SELECT snapshot FROM document_versions
                   WHERE document_name = 'instructions'
                     AND COALESCE(published_at, recorded_at) <= ?
                   ORDER BY COALESCE(published_at, recorded_at) DESC, revision_id DESC LIMIT 1""",
                    (boundary,),
                )
            )
            if not rows or rows[0][0] is None:
                raise HTTPException(
                    status_code=404, detail="Instructions state is unknown at this time"
                )
            snapshot = json.loads(rows[0][0])
            content, variants = snapshot["common"], snapshot["variants"]
        else:
            rows = await db.execute_fetchall(
                "SELECT variant, body FROM skill_variants WHERE name = 'instructions'"
            )
            content = next((row[1] for row in rows if row[0] == "common"), "")
            variants = (
                {row[0]: json.loads(row[1]) for row in rows if row[0] != "common"}
                if harness is not None
                else {}
            )
    if harness is not None:
        if harness not in variants:
            raise HTTPException(
                status_code=404, detail=f"Instructions unavailable for harness {harness!r}"
            )
        content = render(content, variants[harness])
    return Response(content=content, media_type="text/plain")


@router.put("/claude-md")
async def put_claude_md(request: Request):
    content = (await request.body()).decode("utf-8")
    if not content.strip():
        raise HTTPException(status_code=400, detail="CLAUDE.md content cannot be empty")
    async with SKILLS_WRITE_LOCK:
        db = await get_db()
        timestamp = now()
        rows = await db.execute_fetchall(
            "SELECT variant, body FROM skill_variants WHERE name = 'instructions'"
        )
        variants = {row[0]: json.loads(row[1]) for row in rows if row[0] != "common"}
        try:
            validate(content, variants)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            await db.execute(
                """INSERT INTO skills
                       (name, kind, enabled, implicit_invocation, instances, updated_at)
                   VALUES ('instructions', 'instructions', 1, 0, NULL, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       kind = 'instructions', enabled = 1, updated_at = excluded.updated_at""",
                (timestamp,),
            )
            await db.execute(
                """INSERT INTO skill_variants (name, variant, body)
                   VALUES ('instructions', 'common', ?)
                   ON CONFLICT(name, variant) DO UPDATE SET body = excluded.body""",
                (content,),
            )
            await append_revision(
                db,
                content,
                variants,
                published_at=timestamp,
                source="claude-md-api",
                author_instance_id=request.headers.get("X-Instance-Id"),
                author_session_id=request.headers.get("X-Session-Id"),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return {"status": "ok", "updated_at": timestamp}


@router.get("/memory-topics")
async def get_memory_topics() -> list[MemoryTopic]:
    async with MEMORY_WRITE_LOCK:
        db = await get_db()
        rows = await db.execute_fetchall("SELECT * FROM memory_topics ORDER BY slug")
    return [MemoryTopic(**dict(row)) for row in rows]


@router.put("/memory-topics", dependencies=[Depends(require_flow)])
async def put_memory_topics(topics: list[MemoryTopic]):
    slugs = {topic.slug for topic in topics}
    if len(slugs) != len(topics):
        raise HTTPException(status_code=422, detail="Duplicate topic slugs")
    async with MEMORY_WRITE_LOCK:
        db = await get_db()
        rows = await db.execute_fetchall("SELECT topics FROM memories WHERE topics IS NOT NULL")
        referenced = {slug for row in rows for slug in json.loads(row[0])}
        removed = sorted(referenced - slugs)
        if removed:
            raise HTTPException(status_code=409, detail=f"Topic still referenced: {removed[0]}")
        try:
            await db.execute("DELETE FROM memory_topics")
            await db.executemany(
                "INSERT INTO memory_topics (slug, title, description) VALUES (?, ?, ?)",
                [(topic.slug, topic.title, topic.description) for topic in topics],
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return {"status": "ok"}


@router.get("/commands")
async def list_commands() -> dict[str, str]:
    """Return every distributed command as a {name: content} map. The client
    pulls this in one round trip, writes each file, and prunes the rest."""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT name, content FROM config_commands")
    return {row[0]: row[1] for row in rows}


@router.get("/commands/{name}")
async def get_command(name: str):
    db = await get_db()
    rows = list(
        await db.execute_fetchall(
            "SELECT content FROM config_commands WHERE name = ?", (name,)
        )
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Command not found")
    return Response(content=rows[0][0], media_type="text/plain")


@router.put("/commands/{name}")
async def put_command(name: str, request: Request):
    if not _CONFIG_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid command name")
    content = (await request.body()).decode("utf-8")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Command content cannot be empty")
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    db = await get_db()
    await db.execute(
        """INSERT INTO config_commands (name, content, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET content = ?, updated_at = ?""",
        (name, content, now, content, now),
    )
    await db.commit()
    return {"status": "ok", "updated_at": now}


@router.delete("/commands/{name}", status_code=204)
async def delete_command(name: str):
    async with SKILLS_WRITE_LOCK:
        db = await get_db()
        try:
            cursor = await db.execute("DELETE FROM config_commands WHERE name = ?", (name,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Command not found")
            await db.commit()
        except Exception:
            await db.rollback()
            raise


def _hook_row_to_dict(row) -> dict:
    """Shape a config_hooks row for the wire. `instances` is stored as a JSON
    array; a row hand-edited into invalid JSON degrades to None (= every
    machine) rather than breaking the whole pull for one bad row."""
    try:
        instances = json.loads(row[6]) if row[6] else None
    except json.JSONDecodeError:
        instances = None
    try:
        wiring = json.loads(row[7])
    except (json.JSONDecodeError, TypeError):
        wiring = {}
    if not isinstance(wiring, dict):
        wiring = {}
    return {
        "content": row[0],
        "runtime": row[1],
        "event": row[2],
        "matcher": row[3],
        "timeout": row[4],
        "enabled": bool(row[5]),
        "instances": instances,
        "wiring": wiring,
    }


_HOOK_COLUMNS = "content, runtime, event, matcher, timeout, enabled, instances, wiring"


def _render_hook(row, harness: str) -> dict | None:
    item = _hook_row_to_dict(row)
    metadata = item["wiring"].get(harness)
    if not isinstance(metadata, dict):
        return None
    return {
        "content": item["content"],
        "runtime": item["runtime"],
        "event": metadata.get("event"),
        "matcher": metadata.get("matcher"),
        "timeout": metadata.get("timeout"),
        "enabled": item["enabled"],
        "instances": item["instances"],
    }


@router.get("/hooks")
async def list_hooks() -> dict[str, dict]:
    """Return every distributed hook as a {name: spec} map, script body included.
    One round trip, no manifest - same trade as /commands.

    ORDER BY name so two hooks on the same event always render into
    settings.json in the same order. SQLite makes no ordering promise without
    it, and an unstable order would rewrite the file on every pull.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        f"SELECT name, {_HOOK_COLUMNS} FROM config_hooks ORDER BY name"
    )
    result = {}
    for row in rows:
        item = _hook_row_to_dict(row[1:])
        metadata = item["wiring"].get("claude-code")
        if not isinstance(metadata, dict):
            continue
        item.update(metadata)
        result[row[0]] = item
    return result


@router.get("/hooks/render/{harness}")
async def render_hooks(harness: str) -> dict[str, dict]:
    if harness not in ("claude-code", "codex-cli"):
        raise HTTPException(status_code=422, detail=f"Unsupported harness: {harness}")
    db = await get_db()
    rows = await db.execute_fetchall(
        f"SELECT name, {_HOOK_COLUMNS} FROM config_hooks ORDER BY name"
    )
    result = {}
    for row in rows:
        item = _render_hook(row[1:], harness)
        if item is not None:
            result[row[0]] = item
    return result


@router.get("/hooks/{name}")
async def get_hook(name: str):
    db = await get_db()
    rows = list(
        await db.execute_fetchall(
            "SELECT content FROM config_hooks WHERE name = ?", (name,)
        )
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Hook not found")
    return Response(content=rows[0][0], media_type="text/plain")


@router.put("/hooks/{name}")
async def put_hook(name: str, hook: HookUpsert):
    if not _CONFIG_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid hook name")
    if not hook.content.strip():
        raise HTTPException(status_code=400, detail="Hook content cannot be empty")
    instances = json.dumps(hook.instances) if hook.instances is not None else None
    if hook.wiring is None:
        wiring = {
            "claude-code": {
                "event": hook.event,
                "matcher": hook.matcher,
                "timeout": hook.timeout if hook.timeout is not None else 10,
            }
        }
    else:
        wiring = {
            harness: metadata.model_dump()
            for harness, metadata in hook.wiring.items()
        }
    claude = wiring.get("claude-code")
    legacy_event = claude["event"] if claude is not None else ""
    legacy_matcher = claude["matcher"] if claude is not None else None
    legacy_timeout = claude["timeout"] if claude is not None else 10
    stored_wiring = json.dumps(wiring, sort_keys=True)
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    values = (
        hook.content,
        hook.runtime,
        legacy_event,
        legacy_matcher,
        legacy_timeout,
        int(hook.enabled),
        instances,
        stored_wiring,
        now,
    )
    db = await get_db()
    await db.execute(
        f"""INSERT INTO config_hooks (name, {_HOOK_COLUMNS}, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                content = ?, runtime = ?, event = ?, matcher = ?, timeout = ?,
                enabled = ?, instances = ?, wiring = ?, updated_at = ?""",
        (name, *values, *values),
    )
    await db.commit()
    return {"status": "ok", "updated_at": now}


@router.delete("/hooks/{name}", status_code=204)
async def delete_hook(name: str):
    async with SKILLS_WRITE_LOCK:
        db = await get_db()
        try:
            cursor = await db.execute("DELETE FROM config_hooks WHERE name = ?", (name,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Hook not found")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
