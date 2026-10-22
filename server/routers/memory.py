import json
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException

from server.auth import require_auth
from server.db import get_db
from server.models import MemoryCreate, MemoryItem, MemoryUpdate
from server.services.memory_routing import (
    MEMORY_WRITE_LOCK,
    encode_topics,
    memory_item,
    validate_topics,
)

router = APIRouter(
    prefix="/api/memory", tags=["memory"], dependencies=[Depends(require_auth)]
)


def _now() -> str:
    """Keep microseconds because updated_at is stamped into mirror files.

    Two writes to one row must produce two distinct provenance values.
    """
    return datetime.now(UTC).isoformat()


async def require_flow(x_hydra_flow: str = Header(default="")) -> None:
    """Require the memory-write flow tripwire."""
    if not x_hydra_flow.strip() or len(x_hydra_flow) > 64:
        raise HTTPException(
            status_code=428,
            detail=(
                "memory writes belong to a human-gated flow;"
                " rerun with --flow <name>"
            ),
        )


GLOBAL_TYPES = frozenset({"user", "feedback"})
PROJECT_TYPES = frozenset({"project", "reference"})


def _type_for_scope(mem_type: str, project_slug: str | None) -> str:
    """Keep a memory's type consistent with its scope, in BOTH directions.

    Scope is derived from type everywhere (CLI create, dashboard moves), so a
    row whose type and scope disagree has no stable reading. Hence:

    - Pinned (project_slug set) + a global type -> coerced to 'project'. This is
      what auto-scopes the dashboard's Move-to-project.
    - Global (project_slug NULL) + a project-scoped type -> rejected (422). We
      cannot coerce this direction, because there is no way to guess user vs
      feedback - the caller has to say. Silently leaving it would produce a
      global row that sync re-pins to whatever project the next session runs in.
    """
    if project_slug is not None and mem_type in GLOBAL_TYPES:
        return "project"
    if project_slug is None and mem_type in PROJECT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A global memory cannot have type '{mem_type}'; pass a global"
                " type (user or feedback), or pin it to a project."
            ),
        )
    return mem_type


@router.get("")
async def list_memories(
    project_slug: str | None = None,
    include_global: bool = False,
) -> list[MemoryItem]:
    """List memories. Unfiltered returns everything.

    With project_slug: returns memories pinned to that project; optionally
    also global (project_slug IS NULL) memories when include_global=true.
    """
    db = await get_db()
    if project_slug is None:
        rows = await db.execute_fetchall("SELECT * FROM memories ORDER BY id")
    elif include_global:
        rows = await db.execute_fetchall(
            "SELECT * FROM memories WHERE project_slug = ? OR project_slug IS NULL ORDER BY id",
            (project_slug,),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM memories WHERE project_slug = ? ORDER BY id",
            (project_slug,),
        )
    return [memory_item(dict(r)) for r in rows]


@router.get("/snapshot")
async def memory_snapshot(project_slug: str | None = None):
    async with MEMORY_WRITE_LOCK:
        db = await get_db()
        rows = list(
            await db.execute_fetchall(
                """SELECT
                 (SELECT json_group_array(json_object(
                     'slug', slug, 'title', title, 'description', description))
                  FROM (SELECT * FROM memory_topics ORDER BY slug)),
                 (SELECT json_group_array(json_object(
                     'id', id, 'name', name, 'description', description,
                     'type', type, 'body', body, 'project_slug', project_slug,
                     'author_harness', author_harness,
                     'author_session_id', author_session_id, 'author_model', author_model,
                     'topics', json(topics), 'created_at', created_at, 'updated_at', updated_at))
                  FROM (SELECT * FROM memories
                        WHERE project_slug IS NULL OR project_slug = ? ORDER BY id)),
                 EXISTS(SELECT 1 FROM memories)""",
                (project_slug,),
            )
        )
    return {
        "format_version": 1,
        "topics": json.loads(rows[0][0]),
        "memories": json.loads(rows[0][1]),
        "corpus_nonempty": bool(rows[0][2]),
    }


@router.get("/{memory_id}")
async def get_memory(memory_id: int) -> MemoryItem:
    db = await get_db()
    rows = list(await db.execute_fetchall("SELECT * FROM memories WHERE id = ?", (memory_id,)))
    if not rows:
        raise HTTPException(status_code=404, detail="Memory not found")
    return memory_item(dict(rows[0]))


@router.post("", dependencies=[Depends(require_flow)])
async def upsert_memory(memory: MemoryCreate) -> MemoryItem:
    """Upsert on name. Names are globally unique: one name = one memory,
    whatever its scope.

    A POST that would move an existing memory to a different scope is refused
    with 409 unless `rescope` is set: a by-name upsert must never be able to
    silently unpin a memory someone deliberately scoped to a project.
    """
    now = _now()
    mem_type = _type_for_scope(memory.type, memory.project_slug)
    async with MEMORY_WRITE_LOCK:
        db = await get_db()
        await validate_topics(db, memory.topics)
        rows = list(
            await db.execute_fetchall(
                "SELECT id, project_slug FROM memories WHERE name = ?", (memory.name,)
            )
        )
        if rows and rows[0]["project_slug"] != memory.project_slug and not memory.rescope:
            held_by = rows[0]["project_slug"] or "global"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Memory '{memory.name}' already exists in scope '{held_by}';"
                    " memory names are globally unique. Rename it, or pass"
                    " rescope=true to move the existing memory to this scope."
                ),
            )

        sql = (
            "INSERT INTO memories (name, description, type, body, project_slug,"
            " author_harness, author_session_id, author_model, topics, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET description=excluded.description,"
            " type=excluded.type, body=excluded.body,"
            " project_slug=excluded.project_slug,"
            " author_harness=excluded.author_harness,"
            " author_session_id=excluded.author_session_id,"
            " author_model=excluded.author_model, updated_at=excluded.updated_at,"
            " topics=CASE WHEN ? THEN excluded.topics ELSE memories.topics END"
            " RETURNING *"
        )
        params = (
            memory.name,
            memory.description,
            mem_type,
            memory.body,
            memory.project_slug,
            memory.author_harness,
            memory.author_session_id,
            memory.author_model,
            encode_topics(memory.topics),
            now,
            now,
            "topics" in memory.model_fields_set,
        )
        try:
            result = list(await db.execute_fetchall(sql, params))
            await db.commit()
        except sqlite3.IntegrityError as e:
            await db.rollback()
            raise HTTPException(status_code=400, detail=f"Cannot save memory: {e}") from e
        except Exception:
            await db.rollback()
            raise
        return memory_item(dict(result[0]))


@router.put("/{memory_id}", dependencies=[Depends(require_flow)])
async def update_memory(memory_id: int, update: MemoryUpdate) -> MemoryItem:
    async with MEMORY_WRITE_LOCK:
        db = await get_db()
        rows = list(await db.execute_fetchall("SELECT * FROM memories WHERE id = ?", (memory_id,)))
        if not rows:
            raise HTTPException(status_code=404, detail="Memory not found")

        current = dict(rows[0])
        now = _now()
        # exclude_unset, not "drop the Nones": an explicit {"project_slug": null}
        # must be able to unpin a memory to global scope, which is how a re-scope
        # travels without deleting and re-creating the row (and minting a new id).
        author_keys = {"author_harness", "author_session_id", "author_model"}
        fields = update.model_dump(exclude_unset=True, exclude=author_keys)
        if not fields:
            raise HTTPException(status_code=400, detail="No fields to update")
        for key, value in fields.items():
            if value is None and key not in {"project_slug", "topics"}:
                raise HTTPException(status_code=422, detail=f"'{key}' cannot be null")

        # Keep type consistent with scope when either is changing (e.g. pinning a
        # global memory to a project via PUT without sending a new type).
        eff_slug = fields.get("project_slug", current["project_slug"])
        eff_type = fields.get("type", current["type"])
        coerced = _type_for_scope(eff_type, eff_slug)
        if coerced != eff_type:
            fields["type"] = coerced

        if "topics" in fields:
            await validate_topics(db, update.topics)
            fields["topics"] = encode_topics(update.topics)

        if set(fields) != {"topics"}:
            fields.update({key: getattr(update, key) for key in author_keys})
        fields["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = [*fields.values(), memory_id]
        try:
            await db.execute(f"UPDATE memories SET {set_clause} WHERE id = ?", values)
            await db.commit()
        except sqlite3.IntegrityError as e:
            await db.rollback()
            if "UNIQUE" in str(e):
                raise HTTPException(
                    status_code=409,
                    detail=f"Another memory is already named '{fields.get('name')}'",
                ) from e
            raise HTTPException(status_code=400, detail=f"Cannot update memory: {e}") from e
        except Exception:
            await db.rollback()
            raise

        updated = list(
            await db.execute_fetchall("SELECT * FROM memories WHERE id = ?", (memory_id,))
        )
        return memory_item(dict(updated[0]))


@router.delete("/{memory_id}", status_code=204, dependencies=[Depends(require_flow)])
async def delete_memory(memory_id: int):
    async with MEMORY_WRITE_LOCK:
        db = await get_db()
        try:
            cursor = await db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Memory not found")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
