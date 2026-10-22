from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from server.auth import require_auth
from server.db import get_db
from server.models import (
    AutoRegisterRequest,
    AutoRegisterResponse,
    ProjectCreate,
    ProjectItem,
    ProjectPath,
    ProjectUpdate,
)
from server.services.memory_routing import MEMORY_WRITE_LOCK
from server.services.slug import derive_slug_from_cwd, is_contained_by, path_shape

router = APIRouter(
    prefix="/api/projects", tags=["projects"], dependencies=[Depends(require_auth)]
)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


async def _fetch_paths(db, slug: str) -> list[ProjectPath]:
    rows = await db.execute_fetchall(
        "SELECT instance_id, path, auto_registered_at FROM project_paths"
        " WHERE slug = ? ORDER BY instance_id",
        (slug,),
    )
    return [
        ProjectPath(
            instance_id=r["instance_id"],
            path=r["path"],
            auto_registered_at=r["auto_registered_at"],
        )
        for r in rows
    ]


async def _build_item(db, row) -> ProjectItem:
    return ProjectItem(
        slug=row["slug"],
        description=row["description"],
        paths=await _fetch_paths(db, row["slug"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        auto_registered_at=row["auto_registered_at"],
    )


@router.get("")
async def list_projects() -> list[ProjectItem]:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM projects ORDER BY slug")
    return [await _build_item(db, r) for r in rows]


@router.get("/{slug}")
async def get_project(slug: str) -> ProjectItem:
    db = await get_db()
    rows = list(await db.execute_fetchall(
        "SELECT * FROM projects WHERE slug = ?", (slug,)
    ))
    if not rows:
        raise HTTPException(status_code=404, detail="Project not found")
    return await _build_item(db, rows[0])


@router.post("", status_code=201)
async def create_project(
    project: ProjectCreate,
    x_instance_id: str = Header(default="unknown"),
) -> ProjectItem:
    """Idempotent on slug. If the slug is new, creates it. If the slug exists,
    upserts the (slug, instance_id) path - same machine re-registers update the
    row; a new machine adds a new row. Description is only written on create."""
    db = await get_db()
    now = _now()

    rows = list(await db.execute_fetchall(
        "SELECT * FROM projects WHERE slug = ?", (project.slug,)
    ))
    if not rows:
        await db.execute(
            "INSERT INTO projects (slug, description, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (project.slug, project.description, now, now),
        )

    await db.execute(
        "INSERT INTO project_paths (slug, instance_id, path, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(slug, instance_id) DO UPDATE SET"
        "   path = excluded.path, updated_at = excluded.updated_at",
        (project.slug, x_instance_id, project.path, now, now),
    )
    await db.execute(
        "UPDATE projects SET updated_at = ? WHERE slug = ?", (now, project.slug)
    )
    await db.commit()

    rows = list(await db.execute_fetchall(
        "SELECT * FROM projects WHERE slug = ?", (project.slug,)
    ))
    return await _build_item(db, rows[0])


@router.put("/{slug}")
async def update_project(slug: str, update: ProjectUpdate) -> ProjectItem:
    db = await get_db()
    rows = list(await db.execute_fetchall(
        "SELECT * FROM projects WHERE slug = ?", (slug,)
    ))
    if not rows:
        raise HTTPException(status_code=404, detail="Project not found")

    now = _now()
    fields = {k: v for k, v in update.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    fields["updated_at"] = now
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = [*fields.values(), slug]
    await db.execute(f"UPDATE projects SET {set_clause} WHERE slug = ?", values)
    await db.commit()

    rows = list(await db.execute_fetchall(
        "SELECT * FROM projects WHERE slug = ?", (slug,)
    ))
    return await _build_item(db, rows[0])


@router.delete("/{slug}", status_code=204)
async def delete_project(slug: str, force: bool = Query(default=False)):
    db = await get_db()
    # Check before issuing any DML. get_db() is a process-wide connection, so a
    # rollback on a failure path would also discard another request's
    # uncommitted write; SELECT-only failure paths open no transaction.
    rows = list(await db.execute_fetchall(
        "SELECT 1 FROM projects WHERE slug = ?", (slug,)
    ))
    if not rows:
        raise HTTPException(status_code=404, detail="Project not found")

    if force:
        await db.execute("DELETE FROM projects WHERE slug = ?", (slug,))
    else:
        pinned = list(await db.execute_fetchall(
            "SELECT 1 FROM memories WHERE project_slug = ? LIMIT 1", (slug,)
        ))
        if pinned:
            raise HTTPException(
                status_code=409,
                detail="Project still has pinned memories; pass force=true to delete",
            )
        # NOT EXISTS is kept as a guard: if a memory is pinned between the
        # check and here, the delete becomes a no-op rather than unpinning it.
        await db.execute(
            "DELETE FROM projects WHERE slug = ?"
            " AND NOT EXISTS (SELECT 1 FROM memories WHERE project_slug = ?)",
            (slug, slug),
        )
    await db.commit()


@router.delete("/{slug}/paths/{instance_id}", status_code=204)
async def delete_project_path(slug: str, instance_id: str):
    async with MEMORY_WRITE_LOCK:
        db = await get_db()
        try:
            cursor = await db.execute(
                "DELETE FROM project_paths WHERE slug = ? AND instance_id = ?",
                (slug, instance_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Path not registered")
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# --- Auto-registration ---


@router.post("/auto-register")
async def auto_register(
    body: AutoRegisterRequest,
    x_instance_id: str = Header(default="unknown"),
) -> AutoRegisterResponse:
    """Idempotent auto-registration of (cwd, instance_id). Hooks call this on
    SessionStart so new projects appear in the registry without manual setup.

    Server-side policy:
    - Resolve exact paths on this instance, then across all instances.
    - Resolve descendants against confirmed project paths without writing.
    - Otherwise derive a slug from the cwd basename. If the basename hits the
      stoplist or normalizes to nothing usable, return `skipped` with a reason
      and don't write anything.
    - If the slug already exists in projects, attach this machine's path under
      it (status `attached`). The path row gets `auto_registered_at` so the
      dashboard can flag it for review.
    - If the slug is new, create the project and the path row, both flagged
      `auto_registered_at`.
    """
    db = await get_db()
    cwd = body.cwd

    # One fetch; exact matching is compared by shape rather than SQL string
    # equality, so `C:\Work\Repo` and `c:/work/repo` resolve to the same row
    # instead of falling through and minting a peer project.
    rows = list(await db.execute_fetchall(
        "SELECT pp.slug, pp.instance_id, pp.path,"
        " (p.auto_registered_at IS NULL) AS confirmed"
        " FROM project_paths pp JOIN projects p ON p.slug = pp.slug"
        " ORDER BY pp.slug"
    ))
    target = path_shape(cwd)
    same = [row for row in rows if path_shape(row["path"]) == target]

    # Already registered on this instance?
    here = [row for row in same if row["instance_id"] == x_instance_id]
    if here:
        return AutoRegisterResponse(status="existing", slug=here[0]["slug"])

    # Exact paths on another instance still identify the same project.
    if same:
        best = min(same, key=lambda row: (not row["confirmed"], row["slug"]))
        return AutoRegisterResponse(status="existing", slug=best["slug"])

    anchors = [row for row in rows if row["confirmed"]]
    matches = [row for row in anchors if is_contained_by(cwd, row["path"])]
    if matches:
        deepest = max(len(path_shape(row["path"])[1]) for row in matches)
        slugs = {
            row["slug"]
            for row in matches
            if len(path_shape(row["path"])[1]) == deepest
        }
        if len(slugs) > 1:
            return AutoRegisterResponse(
                status="skipped", reason="ambiguous ancestors"
            )
        return AutoRegisterResponse(status="contained", slug=slugs.pop())

    slug, reason = derive_slug_from_cwd(cwd)
    if slug is None:
        return AutoRegisterResponse(status="skipped", reason=reason)

    now = _now()
    # Does the slug already exist?
    existing = list(await db.execute_fetchall(
        "SELECT slug FROM projects WHERE slug = ?", (slug,)
    ))

    if existing:
        # Attach this machine's path. ON CONFLICT updates path; auto flag
        # only set on insert (this branch only runs when there's no row for
        # this (slug, instance_id), since the early-return above covered the
        # case where path matches exactly. A row could still exist with a
        # different path on this instance - treat that as "this machine moved
        # the project", and don't reset the auto flag if it was already cleared
        # by Confirm.)
        await db.execute(
            "INSERT INTO project_paths"
            " (slug, instance_id, path, created_at, updated_at, auto_registered_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(slug, instance_id) DO UPDATE SET"
            "   path = excluded.path,"
            "   updated_at = excluded.updated_at",
            (slug, x_instance_id, cwd, now, now, now),
        )
        await db.execute(
            "UPDATE projects SET updated_at = ? WHERE slug = ?", (now, slug)
        )
        await db.commit()
        return AutoRegisterResponse(status="attached", slug=slug)

    # Brand-new slug: create both project and path with the auto flag.
    await db.execute(
        "INSERT INTO projects"
        " (slug, description, created_at, updated_at, auto_registered_at)"
        " VALUES (?, '', ?, ?, ?)",
        (slug, now, now, now),
    )
    await db.execute(
        "INSERT INTO project_paths"
        " (slug, instance_id, path, created_at, updated_at, auto_registered_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (slug, x_instance_id, cwd, now, now, now),
    )
    await db.commit()
    return AutoRegisterResponse(status="created", slug=slug)


@router.post("/{slug}/confirm", status_code=204)
async def confirm_project(slug: str):
    """Clear the project-level auto_registered_at flag (i.e. reviewed)."""
    async with MEMORY_WRITE_LOCK:
        db = await get_db()
        try:
            cursor = await db.execute(
                "UPDATE projects SET auto_registered_at = NULL WHERE slug = ?", (slug,)
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Project not found")
            await db.commit()
        except Exception:
            await db.rollback()
            raise


@router.post("/{slug}/paths/{instance_id}/confirm", status_code=204)
async def confirm_project_path(slug: str, instance_id: str):
    """Clear the path-level auto_registered_at flag for a specific machine."""
    async with MEMORY_WRITE_LOCK:
        db = await get_db()
        try:
            cursor = await db.execute(
                "UPDATE project_paths SET auto_registered_at = NULL"
                " WHERE slug = ? AND instance_id = ?",
                (slug, instance_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Path not registered")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
