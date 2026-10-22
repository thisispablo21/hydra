import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from server.auth import require_auth
from server.db import get_db
from server.models import SkillUpsert
from server.services.instruction_history import append_revision, now
from server.services.skills import SKILLS_WRITE_LOCK, render, validate

router = APIRouter(
    prefix="/api/config/skills",
    tags=["config"],
    dependencies=[Depends(require_auth)],
)

# Keep skill and harness names under the same filename-safe rule as commands.
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _check_name(value: str, label: str) -> None:
    if not _SKILL_NAME_RE.match(value):
        raise HTTPException(status_code=422, detail=f"Invalid {label} name: {value!r}")


def _decode_slots(body: str | None) -> dict[str, str] | None:
    if body is None:
        return None
    try:
        slots = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(slots, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in slots.items()
    ):
        return None
    return slots


def _decode_instances(body: str | None) -> list[str] | None:
    if body is None:
        return None
    try:
        instances = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(instances, list) or not all(isinstance(item, str) for item in instances):
        return None
    return instances


@router.put("/{name}")
async def put_skill(name: str, skill: SkillUpsert, request: Request):
    _check_name(name, "skill")
    if (skill.kind == "instructions") != (name == "instructions"):
        raise HTTPException(
            status_code=422,
            detail="The name 'instructions' is reserved for kind 'instructions'",
        )
    if not skill.common.strip():
        raise HTTPException(status_code=422, detail="Common body cannot be empty")
    for harness in skill.variants:
        _check_name(harness, "harness")
        if harness == "common":
            raise HTTPException(status_code=422, detail="'common' is not a legal harness")
    try:
        validate(skill.common, skill.variants)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    instances = json.dumps(skill.instances) if skill.instances is not None else None
    async with SKILLS_WRITE_LOCK:
        db = await get_db()
        timestamp = now()
        try:
            await db.execute(
                """INSERT INTO skills
                       (name, kind, enabled, implicit_invocation, instances, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       kind = ?, enabled = ?, implicit_invocation = ?,
                       instances = ?, updated_at = ?""",
                (
                    name,
                    skill.kind,
                    int(skill.enabled),
                    int(skill.implicit_invocation),
                    instances,
                    timestamp,
                    skill.kind,
                    int(skill.enabled),
                    int(skill.implicit_invocation),
                    instances,
                    timestamp,
                ),
            )
            await db.execute("DELETE FROM skill_variants WHERE name = ?", (name,))
            await db.execute(
                "INSERT INTO skill_variants (name, variant, body) VALUES (?, 'common', ?)",
                (name, skill.common),
            )
            for harness, slots in skill.variants.items():
                await db.execute(
                    "INSERT INTO skill_variants (name, variant, body) VALUES (?, ?, ?)",
                    (name, harness, json.dumps(slots)),
                )
            if name == "instructions":
                await append_revision(
                    db,
                    skill.common,
                    skill.variants,
                    published_at=timestamp,
                    source="skills-api",
                    author_instance_id=request.headers.get("X-Instance-Id"),
                    author_session_id=request.headers.get("X-Session-Id"),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return {"status": "ok", "updated_at": timestamp}


@router.delete("/{name}", status_code=204)
async def delete_skill(name: str):
    _check_name(name, "skill")
    if name == "instructions":
        raise HTTPException(status_code=422, detail="Instructions cannot be deleted")
    async with SKILLS_WRITE_LOCK:
        db = await get_db()
        try:
            cursor = await db.execute("DELETE FROM skills WHERE name = ?", (name,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Skill not found")
            await db.commit()
        except Exception:
            await db.rollback()
            raise


@router.get("/{harness}")
async def get_skills(harness: str) -> dict[str, dict]:
    _check_name(harness, "harness")
    # Readers share the writers' connection, so an unlocked SELECT lands inside a
    # publish's open transaction and can miss a skill between its DELETE and commit.
    # A client that sees a nonempty response minus one skill prunes that skill's files.
    async with SKILLS_WRITE_LOCK:
        db = await get_db()
        rows = await db.execute_fetchall(
            """SELECT s.name, s.kind, s.enabled, s.implicit_invocation, s.instances,
                      common.body, harness.body
               FROM skills s
               JOIN skill_variants common
                 ON common.name = s.name AND common.variant = 'common'
               LEFT JOIN skill_variants harness
                 ON harness.name = s.name AND harness.variant = ?
               ORDER BY s.name""",
            (harness,),
        )
    result = {}
    for row in rows:
        rendered = render(row[5], _decode_slots(row[6]))
        filename = "instructions" if row[1] == "instructions" else "SKILL.md"
        result[row[0]] = {
            "kind": row[1],
            "enabled": bool(row[2]),
            "implicit_invocation": bool(row[3]),
            "instances": _decode_instances(row[4]),
            "files": {filename: rendered},
        }
    return result


@router.get("/{name}/{harness}")
async def get_skill(name: str, harness: str):
    _check_name(name, "skill")
    _check_name(harness, "harness")
    async with SKILLS_WRITE_LOCK:
        db = await get_db()
        rows = list(
            await db.execute_fetchall(
                """SELECT s.kind, common.body, harness.body
                   FROM skills s
                   JOIN skill_variants common
                     ON common.name = s.name AND common.variant = 'common'
                   LEFT JOIN skill_variants harness
                     ON harness.name = s.name AND harness.variant = ?
                   WHERE s.name = ?""",
                (harness, name),
            )
        )
    if not rows:
        raise HTTPException(status_code=404, detail="Skill not found")
    row = rows[0]
    body = render(row[1], _decode_slots(row[2]))
    if row[0] == "instructions":
        return Response(content=body, media_type="text/plain")
    return {"SKILL.md": body}
