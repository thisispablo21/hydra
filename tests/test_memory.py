"""Tests for memory CRUD endpoints."""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _create_memory(client: AsyncClient, **overrides) -> dict:
    payload = {
        "name": "test memory",
        "description": "a test",
        "type": "user",
        "body": "some content",
        **overrides,
    }
    res = await client.post("/api/memory", json=payload)
    assert res.status_code == 200
    return res.json()


# --- Flow marker ---


@pytest.mark.parametrize("flow", [None, "   ", "x" * 65])
async def test_create_requires_valid_flow_marker(
    bare_client: AsyncClient, flow: str | None
):
    headers = {"X-Hydra-Flow": flow} if flow is not None else {}
    res = await bare_client.post(
        "/api/memory", json={"name": "flow test", "type": "user"},
        headers=headers,
    )
    assert res.status_code == 428


async def test_create_accepts_valid_flow_marker(bare_client: AsyncClient):
    res = await bare_client.post(
        "/api/memory", json={"name": "flow test", "type": "user"},
        headers={"X-Hydra-Flow": "sync"},
    )
    assert res.status_code == 200


@pytest.mark.parametrize("flow", [None, "   ", "x" * 65])
async def test_update_requires_valid_flow_marker(
    client: AsyncClient, bare_client: AsyncClient, flow: str | None
):
    created = await _create_memory(client, name=f"update {flow!r}")
    headers = {"X-Hydra-Flow": flow} if flow is not None else {}
    res = await bare_client.put(
        f"/api/memory/{created['id']}", json={"body": "changed"},
        headers=headers,
    )
    assert res.status_code == 428


async def test_update_accepts_valid_flow_marker(
    client: AsyncClient, bare_client: AsyncClient
):
    created = await _create_memory(client, name="valid update")
    res = await bare_client.put(
        f"/api/memory/{created['id']}", json={"body": "changed"},
        headers={"X-Hydra-Flow": "sync"},
    )
    assert res.status_code == 200


@pytest.mark.parametrize("flow", [None, "   ", "x" * 65])
async def test_delete_requires_valid_flow_marker(
    client: AsyncClient, bare_client: AsyncClient, flow: str | None
):
    created = await _create_memory(client, name=f"delete {flow!r}")
    headers = {"X-Hydra-Flow": flow} if flow is not None else {}
    res = await bare_client.delete(
        f"/api/memory/{created['id']}", headers=headers
    )
    assert res.status_code == 428


async def test_delete_accepts_valid_flow_marker(
    client: AsyncClient, bare_client: AsyncClient
):
    created = await _create_memory(client, name="valid delete")
    res = await bare_client.delete(
        f"/api/memory/{created['id']}",
        headers={"X-Hydra-Flow": "sync"},
    )
    assert res.status_code == 204


async def test_memory_reads_do_not_require_flow_marker(
    client: AsyncClient, bare_client: AsyncClient
):
    created = await _create_memory(client, name="header-free reads")
    assert (await bare_client.get("/api/memory")).status_code == 200
    assert (
        await bare_client.get(f"/api/memory/{created['id']}")
    ).status_code == 200


# --- List / Get ---


async def test_list_memories_empty(client: AsyncClient):
    res = await client.get("/api/memory")
    assert res.status_code == 200
    assert res.json() == []


async def test_create_and_get_memory(client: AsyncClient):
    created = await _create_memory(client, name="user role", type="user")
    assert created["id"] == 1
    assert created["name"] == "user role"
    assert created["type"] == "user"

    res = await client.get(f"/api/memory/{created['id']}")
    assert res.status_code == 200
    assert res.json()["name"] == "user role"


async def test_memory_authorship_is_last_writer(client: AsyncClient):
    created = await _create_memory(
        client,
        name="authored",
        author_harness="claude-code",
        author_session_id="claude-session",
        author_model="claude-model",
    )
    assert created["author_harness"] == "claude-code"
    assert created["author_session_id"] == "claude-session"
    assert created["author_model"] == "claude-model"

    fetched = (await client.get(f"/api/memory/{created['id']}")).json()
    assert fetched["author_harness"] == "claude-code"

    replaced = await _create_memory(
        client,
        name="authored",
        body="codex write",
        author_harness="codex-cli",
        author_session_id="codex-session",
        author_model="codex-model",
    )
    assert replaced["id"] == created["id"]
    assert replaced["author_harness"] == "codex-cli"
    assert replaced["author_session_id"] == "codex-session"
    assert replaced["author_model"] == "codex-model"

    res = await client.put(
        f"/api/memory/{created['id']}",
        json={
            "body": "claude write",
            "author_harness": "claude-code",
            "author_session_id": "claude-session-2",
            "author_model": "claude-model-2",
        },
    )
    assert res.status_code == 200
    assert res.json()["author_harness"] == "claude-code"
    assert res.json()["author_session_id"] == "claude-session-2"
    assert res.json()["author_model"] == "claude-model-2"

    res = await client.put(
        f"/api/memory/{created['id']}",
        json={
            "body": "codex write again",
            "author_harness": "codex-cli",
            "author_session_id": "codex-session-2",
            "author_model": "codex-model-2",
        },
    )
    assert res.status_code == 200
    assert res.json()["author_session_id"] == "codex-session-2"
    assert res.json()["author_model"] == "codex-model-2"

    rows = (await client.get("/api/memory")).json()
    assert rows[0]["author_harness"] == "codex-cli"
    assert rows[0]["author_session_id"] == "codex-session-2"
    assert rows[0]["author_model"] == "codex-model-2"

    res = await client.put(
        f"/api/memory/{created['id']}", json={"body": "dashboard write"}
    )
    assert res.status_code == 200
    assert res.json()["author_harness"] is None
    assert res.json()["author_session_id"] is None
    assert res.json()["author_model"] is None

    rows = (await client.get("/api/memory")).json()
    assert rows[0]["author_harness"] is None
    assert rows[0]["author_session_id"] is None
    assert rows[0]["author_model"] is None


async def test_list_returns_all(client: AsyncClient):
    await _create_memory(client, name="m1")
    await _create_memory(client, name="m2")

    res = await client.get("/api/memory")
    assert len(res.json()) == 2


# --- Update ---


async def test_update_partial(client: AsyncClient):
    created = await _create_memory(client, name="old name", body="old body")
    mid = created["id"]

    res = await client.put(f"/api/memory/{mid}", json={"name": "new name"})
    assert res.status_code == 200
    updated = res.json()
    assert updated["name"] == "new name"
    assert updated["body"] == "old body"  # unchanged


async def test_description_at_the_cap_is_accepted(client: AsyncClient):
    created = await _create_memory(client, name="capped", description="d" * 200)
    assert len(created["description"]) == 200


@pytest.mark.parametrize("length", [201, 1126])
async def test_create_rejects_description_over_200_chars(client: AsyncClient, length: int):
    res = await client.post(
        "/api/memory",
        json={"name": "too long", "type": "user", "description": "d" * length},
    )
    assert res.status_code == 422
    assert "at most 200 characters" in res.text


async def test_update_rejects_description_over_200_chars(client: AsyncClient):
    created = await _create_memory(client, name="short desc")
    res = await client.put(f"/api/memory/{created['id']}", json={"description": "d" * 201})
    assert res.status_code == 422
    assert "at most 200 characters" in res.text
    res = await client.get(f"/api/memory/{created['id']}")
    assert res.json()["description"] == "a test"


async def test_update_nonexistent_returns_404(client: AsyncClient):
    res = await client.put("/api/memory/999", json={"name": "x"})
    assert res.status_code == 404


async def test_update_empty_body_returns_400(client: AsyncClient):
    created = await _create_memory(client)
    res = await client.put(f"/api/memory/{created['id']}", json={})
    assert res.status_code == 400


async def test_update_with_only_author_fields_returns_400(client: AsyncClient):
    created = await _create_memory(client)
    res = await client.put(
        f"/api/memory/{created['id']}", json={"author_harness": "codex-cli"}
    )
    assert res.status_code == 400


# --- Delete ---


async def test_delete_memory(client: AsyncClient):
    created = await _create_memory(client)
    res = await client.delete(f"/api/memory/{created['id']}")
    assert res.status_code == 204

    res = await client.get(f"/api/memory/{created['id']}")
    assert res.status_code == 404


async def test_delete_nonexistent_returns_404(client: AsyncClient):
    res = await client.delete("/api/memory/999")
    assert res.status_code == 404


# --- Type validation ---


async def test_invalid_type_rejected(client: AsyncClient):
    res = await client.post("/api/memory", json={
        "name": "bad", "type": "invalid", "body": "x",
    })
    assert res.status_code == 422


# --- Upsert (same-name) ---


async def test_upsert_replaces_existing_global(client: AsyncClient):
    first = await _create_memory(client, name="shared", body="v1")
    second = await _create_memory(client, name="shared", body="v2", description="new")
    assert first["id"] == second["id"]
    assert second["body"] == "v2"
    assert second["description"] == "new"

    res = await client.get("/api/memory")
    assert len(res.json()) == 1


async def test_same_name_in_another_scope_returns_409(client: AsyncClient):
    """Names are globally unique. A POST that would land an existing name in a
    different scope is refused - this is what stops a by-name sync push from
    silently unpinning a memory someone deliberately scoped to a project."""
    await _register_project(client, "alpha", "/tmp/alpha")
    await _create_memory(client, name="shared", project_slug="alpha", body="A")

    res = await client.post(
        "/api/memory",
        json={"name": "shared", "type": "feedback", "body": "B"},  # global
    )
    assert res.status_code == 409
    assert "globally unique" in res.json()["detail"]

    res = await client.get("/api/memory")
    rows = res.json()
    assert len(rows) == 1
    assert rows[0]["project_slug"] == "alpha"
    assert rows[0]["body"] == "A"


async def test_post_with_rescope_moves_row_in_place(client: AsyncClient):
    """rescope=true is the explicit opt-in: the existing row moves scope and
    keeps its id (no new row, so no mirror file is orphaned)."""
    await _register_project(client, "alpha", "/tmp/alpha")
    created = await _create_memory(client, name="shared", type="feedback")
    assert created["project_slug"] is None

    moved = await _create_memory(
        client, name="shared", type="feedback", project_slug="alpha", rescope=True,
    )
    assert moved["id"] == created["id"]
    assert moved["project_slug"] == "alpha"
    assert moved["type"] == "project"  # coerced to a project-scoped type

    res = await client.get("/api/memory")
    assert len(res.json()) == 1


async def test_distribute_global_to_multiple_projects(client: AsyncClient):
    """The dashboard's Move-to-projects flow under globally-unique names: each
    target gets its own suffixed name, then the global original is deleted."""
    await _register_project(client, "alpha", "/tmp/alpha")
    await _register_project(client, "beta", "/tmp/beta")
    await _register_project(client, "gamma", "/tmp/gamma")

    g = await _create_memory(client, name="shared", body="GLOBAL", type="user")
    assert g["project_slug"] is None
    global_id = g["id"]

    saved = []
    for slug in ("alpha", "beta", "gamma"):
        row = await _create_memory(
            client, name=f"shared-{slug}", body="GLOBAL", type="user",
            project_slug=slug, rescope=True,
        )
        saved.append(row)

    ids = {s["id"] for s in saved}
    assert len(ids) == 3
    assert global_id not in ids

    res = await client.delete(f"/api/memory/{global_id}")
    assert res.status_code == 204

    res = await client.get("/api/memory")
    rows = res.json()
    assert len(rows) == 3
    assert {row["project_slug"] for row in rows} == {"alpha", "beta", "gamma"}
    assert {row["name"] for row in rows} == {
        "shared-alpha", "shared-beta", "shared-gamma",
    }
    assert all(row["body"] == "GLOBAL" for row in rows)

    res = await client.get(f"/api/memory/{global_id}")
    assert res.status_code == 404


async def test_distribute_overwrites_existing_project_memory(client: AsyncClient):
    """The dashboard relies on upsert semantics when a collision is confirmed:
    POSTing the same (name, project_slug) replaces the row in place."""
    await _register_project(client, "alpha", "/tmp/alpha")
    pre = await _create_memory(client, name="shared", project_slug="alpha", body="OLD")
    over = await _create_memory(client, name="shared", project_slug="alpha", body="NEW")
    assert pre["id"] == over["id"]
    assert over["body"] == "NEW"


# --- Type <-> scope coercion ---


async def test_pinned_global_type_coerced_to_project(client: AsyncClient):
    """A global type pinned to a project is auto-scoped to 'project'. Covers the
    dashboard Move/Copy-to-project flow and guards against a pinned-but-global
    row that `hydra sync` would re-globalize into a duplicate."""
    await _register_project(client, "alpha", "/tmp/alpha")
    row = await _create_memory(
        client, name="shared", type="feedback", project_slug="alpha",
    )
    assert row["project_slug"] == "alpha"
    assert row["type"] == "project"


async def test_pinned_reference_type_preserved(client: AsyncClient):
    """reference is already a project-scoped type and must pass through."""
    await _register_project(client, "alpha", "/tmp/alpha")
    row = await _create_memory(
        client, name="ref", type="reference", project_slug="alpha",
    )
    assert row["type"] == "reference"


async def test_global_type_preserved_when_unpinned(client: AsyncClient):
    """No coercion for global memories - user vs feedback is the caller's call."""
    row = await _create_memory(client, name="g", type="feedback")
    assert row["project_slug"] is None
    assert row["type"] == "feedback"


async def test_update_pinning_coerces_type(client: AsyncClient):
    """Pinning a global memory via PUT (scope changes, type not sent) coerces
    the type to 'project'."""
    await _register_project(client, "alpha", "/tmp/alpha")
    created = await _create_memory(client, name="m", type="feedback")
    assert created["type"] == "feedback"

    res = await client.put(
        f"/api/memory/{created['id']}", json={"project_slug": "alpha"},
    )
    assert res.status_code == 200
    updated = res.json()
    assert updated["project_slug"] == "alpha"
    assert updated["type"] == "project"


async def test_update_unpins_to_global_with_explicit_null(client: AsyncClient):
    """An explicit project_slug=null unpins. Only reachable because the PUT uses
    exclude_unset - dropping every None would make an unpin unexpressible, which
    is what forced re-scopes through delete + re-create (and minted duplicates)."""
    await _register_project(client, "alpha", "/tmp/alpha")
    created = await _create_memory(
        client, name="m", type="project", project_slug="alpha",
    )

    res = await client.put(
        f"/api/memory/{created['id']}",
        json={"project_slug": None, "type": "feedback"},
    )
    assert res.status_code == 200
    updated = res.json()
    assert updated["id"] == created["id"]  # same row, no new id
    assert updated["project_slug"] is None
    assert updated["type"] == "feedback"


async def test_unpin_without_a_global_type_is_rejected(client: AsyncClient):
    """`hydra sync` derives scope FROM type, so a global row holding a project
    type is unstable - the next Stop hook re-pins it to whatever project that
    session is in. The server cannot guess user vs feedback, so it refuses."""
    await _register_project(client, "alpha", "/tmp/alpha")
    created = await _create_memory(
        client, name="m", type="project", project_slug="alpha",
    )

    res = await client.put(f"/api/memory/{created['id']}", json={"project_slug": None})
    assert res.status_code == 422

    res = await client.get(f"/api/memory/{created['id']}")
    assert res.json()["project_slug"] == "alpha"  # unchanged

    # With a global type supplied, it works.
    res = await client.put(
        f"/api/memory/{created['id']}",
        json={"project_slug": None, "type": "feedback"},
    )
    assert res.status_code == 200
    assert res.json()["project_slug"] is None
    assert res.json()["type"] == "feedback"


async def test_global_memory_cannot_hold_a_project_type(client: AsyncClient):
    res = await client.post("/api/memory", json={"name": "m", "type": "project"})
    assert res.status_code == 422
    res = await client.post("/api/memory", json={"name": "m", "type": "reference"})
    assert res.status_code == 422


async def test_update_rejects_explicit_null_on_other_fields(client: AsyncClient):
    created = await _create_memory(client, name="m")
    res = await client.put(f"/api/memory/{created['id']}", json={"name": None})
    assert res.status_code == 422


async def test_update_rename_onto_existing_name_returns_409(client: AsyncClient):
    await _create_memory(client, name="first")
    second = await _create_memory(client, name="second")

    res = await client.put(f"/api/memory/{second['id']}", json={"name": "first"})
    assert res.status_code == 409

    res = await client.get("/api/memory")
    assert sorted(m["name"] for m in res.json()) == ["first", "second"]


async def test_consecutive_writes_produce_distinct_updated_at(client: AsyncClient):
    """updated_at is the version token `hydra sync` uses to detect that a memory
    changed on the server since a mirror file was written. Two writes to one row
    must never collide, or a stale mirror silently reverts the newer one - which
    a whole-second timestamp did, for any two writes inside the same second."""
    created = await _create_memory(client, name="m", body="v1")

    stamps = {created["updated_at"]}
    for i in range(5):
        res = await client.put(f"/api/memory/{created['id']}", json={"body": f"v{i}"})
        assert res.status_code == 200
        stamps.add(res.json()["updated_at"])

    assert len(stamps) == 6, "updated_at collided across writes"


async def test_upsert_unknown_project_slug_returns_400(client: AsyncClient):
    res = await client.post(
        "/api/memory",
        json={"name": "m", "type": "project", "project_slug": "nope"},
    )
    assert res.status_code == 400


# --- Filtered list ---


async def test_list_filtered_by_project(client: AsyncClient):
    await _register_project(client, "alpha", "/tmp/alpha")
    await _create_memory(client, name="g1", type="user")  # global
    await _create_memory(client, name="p1", type="project", project_slug="alpha")

    res = await client.get("/api/memory?project_slug=alpha")
    names = [m["name"] for m in res.json()]
    assert names == ["p1"]


async def test_list_filtered_by_project_with_globals(client: AsyncClient):
    await _register_project(client, "alpha", "/tmp/alpha")
    await _register_project(client, "beta", "/tmp/beta")
    await _create_memory(client, name="g1", type="user")
    await _create_memory(client, name="p_alpha", type="project", project_slug="alpha")
    await _create_memory(client, name="p_beta", type="project", project_slug="beta")

    res = await client.get("/api/memory?project_slug=alpha&include_global=true")
    names = sorted(m["name"] for m in res.json())
    assert names == ["g1", "p_alpha"]


# --- Auth ---


async def test_memory_auth_required(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("server.config.AUTH_TOKEN", "secret")
    res = await client.get("/api/memory")
    assert res.status_code == 401


async def test_memory_write_auth_runs_before_flow(
    bare_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("server.config.AUTH_TOKEN", "secret")
    res = await bare_client.post(
        "/api/memory", json={"name": "unauthorized", "type": "user"}
    )
    assert res.status_code == 401


# --- Helpers ---


async def _register_project(client: AsyncClient, slug: str, path: str) -> None:
    res = await client.post("/api/projects", json={"slug": slug, "path": path})
    assert res.status_code == 201
