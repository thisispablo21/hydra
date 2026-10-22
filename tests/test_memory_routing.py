import asyncio

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

CATALOG = [{"slug": "debug", "title": "Debug", "description": "Diagnose problems"}]


async def test_routing_omission_null_empty_and_replacement(client: AsyncClient):
    assert (await client.put("/api/config/memory-topics", json=CATALOG)).status_code == 200
    authors = {"author_harness": "h", "author_session_id": "original", "author_model": "model"}
    payload = {"name": "m", "type": "user", **authors}
    created = (await client.post("/api/memory", json=payload)).json()
    assert created["topics"] is None
    url = f"/api/memory/{created['id']}"
    routed = (await client.put(url, json={"topics": ["debug"]})).json()
    assert {key: routed[key] for key in authors} == authors
    for topics in (["debug"], [], None):
        response = await client.put(url, json={"topics": topics})
        assert response.status_code == 200
        assert response.json()["topics"] == topics
        legacy = await client.post("/api/memory", json={"name": "m", "type": "user", "body": "v2"})
        assert legacy.json()["topics"] == topics
        assert (await client.put(url, json={"body": "v3"})).json()["topics"] == topics
    assert (
        await client.post("/api/memory", json={"name": "m", "type": "user", "topics": []})
    ).json()["topics"] == []


@pytest.mark.parametrize("topics", [["missing"], ["../debug"], ["debug", "debug"], ["catalog"]])
async def test_membership_validation(client: AsyncClient, topics):
    await client.put("/api/config/memory-topics", json=CATALOG)
    res = await client.post("/api/memory", json={"name": "m", "type": "user", "topics": topics})
    assert res.status_code == 422
    assert (await client.get("/api/memory")).json() == []


async def test_catalog_validation_and_referenced_delete(client: AsyncClient):
    for catalog in (
        [CATALOG[0], CATALOG[0]],
        [{**CATALOG[0], "slug": "catalog"}],
        [{**CATALOG[0], "slug": "../escape"}],
        [{**CATALOG[0], "title": " "}],
        [{**CATALOG[0], "description": ""}],
    ):
        assert (await client.put("/api/config/memory-topics", json=catalog)).status_code == 422
    await client.put("/api/config/memory-topics", json=CATALOG)
    created = (
        await client.post("/api/memory", json={"name": "m", "type": "user", "topics": ["debug"]})
    ).json()
    assert (await client.put("/api/config/memory-topics", json=[])).status_code == 409
    assert (await client.get("/api/config/memory-topics")).json() == CATALOG
    await client.put(f"/api/memory/{created['id']}", json={"topics": []})
    assert (await client.put("/api/config/memory-topics", json=[])).status_code == 200


async def test_catalog_auth_and_flow(client: AsyncClient, bare_client: AsyncClient, monkeypatch):
    assert (await bare_client.put("/api/config/memory-topics", json=CATALOG)).status_code == 428
    monkeypatch.setattr("server.config.AUTH_TOKEN", "secret")
    assert (await client.get("/api/config/memory-topics")).status_code == 401
    assert (await bare_client.put("/api/config/memory-topics", json=CATALOG)).status_code == 401
    assert (await client.get("/api/memory/snapshot")).status_code == 401


async def test_snapshot_scope_and_corpus_authority(client: AsyncClient):
    assert (await client.get("/api/memory/snapshot")).json() == {
        "format_version": 1,
        "topics": [],
        "memories": [],
        "corpus_nonempty": False,
    }
    await client.put("/api/config/memory-topics", json=CATALOG)
    for slug in ("alpha", "beta"):
        await client.post("/api/projects", json={"slug": slug, "path": f"/tmp/{slug}"})
        await client.post(
            "/api/memory",
            json={"name": slug, "type": "project", "project_slug": slug, "topics": []},
        )
    global_snapshot = (await client.get("/api/memory/snapshot")).json()
    assert global_snapshot["memories"] == []
    assert global_snapshot["corpus_nonempty"] is True
    await client.post("/api/memory", json={"name": "global", "type": "user"})
    snapshot = (await client.get("/api/memory/snapshot?project_slug=alpha")).json()
    assert snapshot["topics"] == CATALOG
    assert {row["name"] for row in snapshot["memories"]} == {"global", "alpha"}
    assert snapshot["memories"][0]["topics"] == []
    assert len((await client.get("/api/memory")).json()) == 3


async def test_concurrent_catalog_removal_never_orphans_membership(client: AsyncClient):
    for iteration in range(8):
        await client.put("/api/config/memory-topics", json=CATALOG)
        created, removed = await asyncio.gather(
            client.post(
                "/api/memory", json={"name": f"m{iteration}", "type": "user", "topics": ["debug"]}
            ),
            client.put("/api/config/memory-topics", json=[]),
        )
        assert (created.status_code, removed.status_code) in ((200, 409), (422, 200))
        snapshot = (await client.get("/api/memory/snapshot")).json()
        known = {topic["slug"] for topic in snapshot["topics"]}
        assert all(set(memory["topics"] or []) <= known for memory in snapshot["memories"])
        if created.status_code == 200:
            await client.delete(f"/api/memory/{created.json()['id']}")


async def test_snapshot_waits_for_shared_write_lock(client: AsyncClient, monkeypatch):
    from server.db import get_db

    waiting = asyncio.Event()

    class ObservedLock(asyncio.Lock):
        async def acquire(self):
            if self.locked():
                waiting.set()
            return await super().acquire()

    lock = ObservedLock()
    monkeypatch.setattr("server.routers.memory.MEMORY_WRITE_LOCK", lock)
    monkeypatch.setattr("server.routers.config.MEMORY_WRITE_LOCK", lock)
    await client.put("/api/config/memory-topics", json=CATALOG)
    await client.post("/api/memory", json={"name": "m", "type": "user", "topics": ["debug"]})
    db = await get_db()
    async with lock:
        await db.execute("UPDATE memories SET topics = '[]'")
        await db.execute("DELETE FROM memory_topics")
        pending = asyncio.create_task(client.get("/api/memory/snapshot"))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        assert not pending.done()
        await db.commit()
    snapshot = (await pending).json()
    assert snapshot["topics"] == []
    assert snapshot["memories"][0]["topics"] == []
