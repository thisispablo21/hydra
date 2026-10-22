"""Tests for project registry endpoints."""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _create_project(
    client: AsyncClient, *, instance_id: str = "host-1", **overrides
) -> dict:
    payload = {
        "slug": "hydra",
        "path": "/home/user/projects/hydra",
        "description": "Claude Code control plane",
        **overrides,
    }
    res = await client.post(
        "/api/projects", json=payload, headers={"X-Instance-Id": instance_id}
    )
    assert res.status_code == 201
    return res.json()


# --- List / Get ---


async def test_list_projects_empty(client: AsyncClient):
    res = await client.get("/api/projects")
    assert res.status_code == 200
    assert res.json() == []


async def test_create_and_get_project(client: AsyncClient):
    created = await _create_project(client)
    assert created["slug"] == "hydra"
    assert created["paths"] == [{
        "instance_id": "host-1",
        "path": "/home/user/projects/hydra",
        "auto_registered_at": None,
    }]
    assert "path" not in created  # top-level path removed
    assert created["auto_registered_at"] is None

    res = await client.get("/api/projects/hydra")
    assert res.status_code == 200
    body = res.json()
    assert body["slug"] == "hydra"
    assert body["paths"][0]["instance_id"] == "host-1"


async def test_list_returns_all(client: AsyncClient):
    await _create_project(client, slug="hydra", path="/p/hydra")
    await _create_project(client, slug="webapp", path="/p/webapp")

    res = await client.get("/api/projects")
    assert len(res.json()) == 2


# --- Multi-path registration ---


async def test_same_slug_different_machine_adds_path(client: AsyncClient):
    """Registering the same slug from a second machine appends a path row
    rather than returning 409."""
    await _create_project(
        client, instance_id="vps", path="/home/giosue/projects/hydra"
    )
    res = await client.post(
        "/api/projects",
        json={"slug": "hydra", "path": "C:\\Users\\giosu\\projects\\hydra"},
        headers={"X-Instance-Id": "laptop"},
    )
    assert res.status_code == 201
    body = res.json()
    paths = {p["instance_id"]: p["path"] for p in body["paths"]}
    assert paths == {
        "vps": "/home/giosue/projects/hydra",
        "laptop": "C:\\Users\\giosu\\projects\\hydra",
    }


async def test_same_slug_same_machine_updates_path(client: AsyncClient):
    """Same (slug, instance_id) with a new path updates in place."""
    await _create_project(client, instance_id="host-1", path="/old/path")
    res = await client.post(
        "/api/projects",
        json={"slug": "hydra", "path": "/new/path"},
        headers={"X-Instance-Id": "host-1"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["paths"] == [{
        "instance_id": "host-1",
        "path": "/new/path",
        "auto_registered_at": None,
    }]


async def test_delete_single_path(client: AsyncClient):
    await _create_project(client, instance_id="vps", path="/srv/hydra")
    await _create_project(client, instance_id="laptop", path="/Users/x/hydra")

    res = await client.delete("/api/projects/hydra/paths/laptop")
    assert res.status_code == 204

    body = (await client.get("/api/projects/hydra")).json()
    assert [p["instance_id"] for p in body["paths"]] == ["vps"]


async def test_delete_path_not_found(client: AsyncClient):
    await _create_project(client)
    res = await client.delete("/api/projects/hydra/paths/nope")
    assert res.status_code == 404


# --- Update ---


async def test_update_description(client: AsyncClient):
    await _create_project(client)
    res = await client.put("/api/projects/hydra", json={"description": "updated"})
    assert res.status_code == 200
    assert res.json()["description"] == "updated"


async def test_update_nonexistent_returns_404(client: AsyncClient):
    res = await client.put("/api/projects/nope", json={"description": "x"})
    assert res.status_code == 404


async def test_update_empty_body_returns_400(client: AsyncClient):
    await _create_project(client)
    res = await client.put("/api/projects/hydra", json={})
    assert res.status_code == 400


# --- Delete ---


async def test_delete_project(client: AsyncClient):
    await _create_project(client)
    res = await client.delete("/api/projects/hydra")
    assert res.status_code == 204

    res = await client.get("/api/projects/hydra")
    assert res.status_code == 404


async def test_delete_cascades_paths(client: AsyncClient):
    """Deleting the project also removes its path rows (FK ON DELETE CASCADE)."""
    await _create_project(client, instance_id="a", path="/a")
    await _create_project(client, instance_id="b", path="/b")
    res = await client.delete("/api/projects/hydra")
    assert res.status_code == 204

    # Re-create and verify no stale paths linger
    recreated = await _create_project(client, instance_id="c", path="/c")
    assert recreated["paths"] == [{
        "instance_id": "c", "path": "/c", "auto_registered_at": None,
    }]


async def test_delete_nonexistent_returns_404(client: AsyncClient):
    res = await client.delete("/api/projects/nope")
    assert res.status_code == 404


async def test_delete_project_with_pinned_memory_requires_force(client: AsyncClient):
    await _create_project(client)
    memory = await client.post("/api/memory", json={
        "name": "project note",
        "type": "project",
        "project_slug": "hydra",
    })
    assert memory.status_code == 200

    blocked = await client.delete("/api/projects/hydra")
    assert blocked.status_code == 409
    assert (await client.get("/api/projects/hydra")).status_code == 200
    assert (await client.get(f"/api/memory/{memory.json()['id']}")).json()[
        "project_slug"
    ] == "hydra"

    forced = await client.delete("/api/projects/hydra?force=true")
    assert forced.status_code == 204
    assert (await client.get(f"/api/memory/{memory.json()['id']}")).json()[
        "project_slug"
    ] is None


# --- Auth ---


async def test_projects_auth_required(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("server.config.AUTH_TOKEN", "secret")
    res = await client.get("/api/projects")
    assert res.status_code == 401


# --- Auto-register ---


async def _auto_register(client: AsyncClient, cwd: str, instance_id: str = "host-1"):
    return await client.post(
        "/api/projects/auto-register",
        json={"cwd": cwd},
        headers={"X-Instance-Id": instance_id},
    )


async def test_auto_register_creates_new_slug(client: AsyncClient):
    res = await _auto_register(client, "/home/giosue/projects/scratchpad")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "created"
    assert body["slug"] == "scratchpad"

    proj = (await client.get("/api/projects/scratchpad")).json()
    assert proj["auto_registered_at"] is not None
    assert proj["paths"][0]["instance_id"] == "host-1"
    assert proj["paths"][0]["auto_registered_at"] is not None


async def test_auto_register_attaches_to_existing_slug(client: AsyncClient):
    """Slug 'hydra' exists from machine A; machine B auto-registers a cwd
    whose basename matches → only the path row is flagged, the project
    record's auto flag stays clear (since it was manually created)."""
    await _create_project(
        client, instance_id="vps", path="/home/giosue/projects/hydra"
    )
    res = await _auto_register(
        client, r"C:\Users\giosu\projects\hydra", instance_id="laptop"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "attached"
    assert body["slug"] == "hydra"

    proj = (await client.get("/api/projects/hydra")).json()
    assert proj["auto_registered_at"] is None  # manually created, untouched
    laptop_path = next(p for p in proj["paths"] if p["instance_id"] == "laptop")
    assert laptop_path["auto_registered_at"] is not None
    vps_path = next(p for p in proj["paths"] if p["instance_id"] == "vps")
    assert vps_path["auto_registered_at"] is None


async def test_auto_register_idempotent_on_existing_path(client: AsyncClient):
    """Same machine + same cwd called twice returns 'existing' on the
    second call without writing again."""
    first = await _auto_register(client, "/home/giosue/projects/foo")
    assert first.json()["status"] == "created"
    second = await _auto_register(client, "/home/giosue/projects/foo")
    assert second.status_code == 200
    assert second.json() == {
        "status": "existing", "slug": "foo", "reason": None,
    }


async def test_auto_register_contained_cwd_writes_no_path(client: AsyncClient):
    await _create_project(client, path="/srv/hydra")
    res = await _auto_register(client, "/srv/hydra/server/services")

    assert res.json() == {
        "status": "contained", "slug": "hydra", "reason": None,
    }
    project = (await client.get("/api/projects/hydra")).json()
    assert project["paths"] == [{
        "instance_id": "host-1",
        "path": "/srv/hydra",
        "auto_registered_at": None,
    }]


async def test_auto_register_exact_across_instances_beats_containment(
    client: AsyncClient,
):
    await _create_project(
        client, slug="outer", path="/work/outer-repo", instance_id="a"
    )
    await _create_project(
        client, slug="inner", path="/work/outer-repo/inner", instance_id="a"
    )

    res = await _auto_register(client, "/work/outer-repo/inner", instance_id="b")

    assert res.json() == {
        "status": "existing", "slug": "inner", "reason": None,
    }
    inner = (await client.get("/api/projects/inner")).json()
    assert [path["instance_id"] for path in inner["paths"]] == ["a"]


async def test_auto_register_exact_prefers_confirmed_project(client: AsyncClient):
    first = await _auto_register(client, "/srv/shared", instance_id="auto")
    assert first.json()["status"] == "created"
    await _create_project(
        client, slug="confirmed", path="/srv/shared", instance_id="manual"
    )

    res = await _auto_register(client, "/srv/shared", instance_id="third")

    assert res.json() == {
        "status": "existing", "slug": "confirmed", "reason": None,
    }


async def test_auto_register_exact_match_is_shape_not_string(client: AsyncClient):
    """A Windows path spelled with a different drive case or separator is the
    same path, so it resolves instead of minting a peer project."""
    await _create_project(
        client, slug="winproj", path=r"C:\Work\Repo", instance_id="a"
    )

    res = await _auto_register(client, "c:/work/repo", instance_id="b")

    assert res.json() == {
        "status": "existing", "slug": "winproj", "reason": None,
    }
    listed = (await client.get("/api/projects")).json()
    assert [p["slug"] for p in listed] == ["winproj"]


async def test_auto_register_posix_exact_match_stays_case_sensitive(
    client: AsyncClient,
):
    """POSIX paths are case-sensitive, so /Work/Repo is a different directory."""
    await _create_project(
        client, slug="posixproj", path="/Work/Repo", instance_id="a"
    )

    res = await _auto_register(client, "/work/repo", instance_id="b")

    assert res.json()["status"] == "created"


async def test_auto_register_exact_uses_lowest_unconfirmed_slug(client: AsyncClient):
    for slug in ("zeta", "alpha"):
        created = await _auto_register(client, f"/srv/{slug}", instance_id=slug)
        assert created.json()["status"] == "created"
        await _create_project(
            client, slug=slug, path="/srv/shared", instance_id=slug
        )

    res = await _auto_register(client, "/srv/shared", instance_id="third")

    assert res.json() == {
        "status": "existing", "slug": "alpha", "reason": None,
    }


async def test_auto_register_deepest_confirmed_ancestor_wins(client: AsyncClient):
    await _create_project(client, slug="outer", path="/work/repo")
    await _create_project(client, slug="inner", path="/work/repo/packages/app")

    res = await _auto_register(client, "/work/repo/packages/app/src")

    assert res.json() == {
        "status": "contained", "slug": "inner", "reason": None,
    }


async def test_auto_register_ambiguous_ancestors_skips_without_write(
    client: AsyncClient,
):
    await _create_project(client, slug="alpha", path="/work/repo", instance_id="a")
    await _create_project(client, slug="beta", path="/work/repo", instance_id="b")

    res = await _auto_register(client, "/work/repo/src", instance_id="third")

    assert res.json() == {
        "status": "skipped", "slug": None, "reason": "ambiguous ancestors",
    }
    projects = (await client.get("/api/projects")).json()
    assert [project["slug"] for project in projects] == ["alpha", "beta"]


async def test_auto_register_unconfirmed_project_does_not_anchor(client: AsyncClient):
    first = await _auto_register(client, "/srv/junk")
    assert first.json()["status"] == "created"

    child = await _auto_register(client, "/srv/junk/child", instance_id="other")

    assert child.json() == {
        "status": "created", "slug": "child", "reason": None,
    }


async def test_auto_register_containment_crosses_instances(client: AsyncClient):
    await _create_project(
        client, slug="shared", path="/cluster/home/u/repo", instance_id="m1"
    )

    res = await _auto_register(
        client, "/cluster/home/u/repo/src", instance_id="m2"
    )

    assert res.json() == {
        "status": "contained", "slug": "shared", "reason": None,
    }


async def test_auto_register_project_confirmation_anchors_flagged_path(
    client: AsyncClient,
):
    await _create_project(client, slug="hydra", path="/srv/hydra", instance_id="linux")
    attached = await _auto_register(
        client, r"C:\work\hydra", instance_id="windows"
    )
    assert attached.json()["status"] == "attached"

    res = await _auto_register(
        client, r"C:\work\hydra\server", instance_id="windows-2"
    )

    assert res.json() == {
        "status": "contained", "slug": "hydra", "reason": None,
    }


async def test_auto_register_skips_stoplist(client: AsyncClient):
    res = await _auto_register(client, "/home/giosue/Downloads")
    assert res.status_code == 200
    assert res.json()["status"] == "skipped"
    assert "stoplist" in res.json()["reason"]
    # Nothing was written
    assert (await client.get("/api/projects")).json() == []


async def test_auto_register_skips_root_paths(client: AsyncClient):
    res = await _auto_register(client, "/")
    assert res.json()["status"] == "skipped"
    assert (await client.get("/api/projects")).json() == []


async def test_auto_register_normalizes_slug(client: AsyncClient):
    """Spaces and mixed case in basename → normalized slug."""
    res = await _auto_register(client, "/home/giosue/My Stuff")
    assert res.json() == {"status": "created", "slug": "my-stuff", "reason": None}


# --- Confirm ---


async def test_confirm_project_clears_flag(client: AsyncClient):
    await _auto_register(client, "/home/giosue/projects/scratch")
    res = await client.post("/api/projects/scratch/confirm")
    assert res.status_code == 204
    proj = (await client.get("/api/projects/scratch")).json()
    assert proj["auto_registered_at"] is None
    # Path-level flag is independent and untouched
    assert proj["paths"][0]["auto_registered_at"] is not None


async def test_confirm_path_clears_flag(client: AsyncClient):
    await _create_project(client, instance_id="vps", path="/srv/hydra")
    await _auto_register(
        client, r"C:\Users\giosu\projects\hydra", instance_id="laptop"
    )
    res = await client.post("/api/projects/hydra/paths/laptop/confirm")
    assert res.status_code == 204
    proj = (await client.get("/api/projects/hydra")).json()
    laptop = next(p for p in proj["paths"] if p["instance_id"] == "laptop")
    assert laptop["auto_registered_at"] is None


async def test_confirm_nonexistent_returns_404(client: AsyncClient):
    res = await client.post("/api/projects/nope/confirm")
    assert res.status_code == 404
    res = await client.post("/api/projects/nope/paths/host/confirm")
    assert res.status_code == 404


async def test_manual_create_has_no_auto_flag(client: AsyncClient):
    """The pre-existing manual-create endpoint stays unflagged."""
    await _create_project(client)
    proj = (await client.get("/api/projects/hydra")).json()
    assert proj["auto_registered_at"] is None
    assert proj["paths"][0]["auto_registered_at"] is None


@pytest.mark.parametrize(
    "method,path",
    [
        ("delete", "/api/projects/missing/paths/missing"),
        ("post", "/api/projects/missing/confirm"),
        ("post", "/api/projects/missing/paths/missing/confirm"),
        ("delete", "/api/config/commands/missing"),
        ("delete", "/api/config/hooks/missing"),
        ("delete", "/api/config/skills/missing"),
    ],
)
@pytest.mark.parametrize("next_write", ["memory", "catalog", "instructions"])
async def test_mutation_404_ends_transaction_before_next_write(
    client: AsyncClient, method: str, path: str, next_write: str
):
    import asyncio

    from server.db import get_db

    response = await client.request(method, path)
    assert response.status_code == 404
    db = await get_db()
    assert not db.in_transaction
    if next_write == "memory":
        operation = client.post("/api/memory", json={"name": "after404", "type": "user"})
    elif next_write == "catalog":
        operation = client.put("/api/config/memory-topics", json=[])
    else:
        operation = client.put("/api/config/claude-md", content="After 404\n")
    assert (await asyncio.wait_for(operation, timeout=3)).status_code == 200
    assert not db.in_transaction
