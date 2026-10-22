import asyncio
import json
from datetime import datetime

import pytest
from httpx import AsyncClient

from server import db as db_module

pytestmark = pytest.mark.asyncio


async def publish(client, common="Hello {{who}}\n", who="Claude"):
    return await client.put(
        "/api/config/skills/instructions",
        json={"kind": "instructions", "common": common, "variants": {"claude-code": {"who": who}}},
    )


async def test_both_publication_routes_snapshot_slots_and_authorship(client: AsyncClient):
    first = await publish(client)
    assert first.status_code == 200
    second = await client.put(
        "/api/config/claude-md",
        content="New {{who}}\n",
        headers={"X-Instance-Id": "machine", "X-Session-Id": "session"},
    )
    assert second.status_code == 200
    await publish(client, "Later {{who}}\n", "Other")
    history = (await client.get("/api/config/claude-md/history")).json()
    assert len(history) == 3
    assert history[1]["author_instance_id"] == "machine"
    assert history[1]["author_session_id"] == "session"
    assert history[1]["source"] == "claude-md-api"
    for at, expected in (
        (first.json()["updated_at"], "Hello Claude\n"),
        (second.json()["updated_at"], "New Claude\n"),
    ):
        result = await client.get(
            "/api/config/claude-md", params={"at": at, "harness": "claude-code"}
        )
        assert result.text == expected
    assert (await client.get("/api/config/claude-md")).text == "Later {{who}}\n"
    historical = await client.get(
        "/api/config/claude-md", params={"at": first.json()["updated_at"]}
    )
    assert historical.text == "Hello {{who}}\n"


async def test_identical_slots_only_publications_are_retained(client: AsyncClient):
    await publish(client)
    await publish(client)
    third = await publish(client, who="Codex")
    rows = (await client.get("/api/config/claude-md/history")).json()
    assert len(rows) == 3
    assert len({row["revision_id"] for row in rows}) == 3
    rendered = await client.get(
        "/api/config/claude-md", params={"at": third.json()["updated_at"], "harness": "claude-code"}
    )
    assert rendered.text == "Hello Codex\n"


async def test_invalid_publication_appends_nothing_and_keeps_document(client: AsyncClient):
    await publish(client)
    for method, body in (("common", "Bad {{missing}}"), ("full", "")):
        if method == "common":
            res = await client.put("/api/config/claude-md", content=body)
        else:
            res = await publish(client, body)
        assert res.status_code == 422
    assert len((await client.get("/api/config/claude-md/history")).json()) == 1
    assert (await client.get("/api/config/claude-md")).text == "Hello {{who}}\n"


async def test_history_validation_timezone_and_missing_harness(client: AsyncClient):
    first = await publish(client)
    for at in ("garbage", "2026-01-01T00:00:00"):
        assert (await client.get("/api/config/claude-md", params={"at": at})).status_code == 422
    assert (
        await client.get("/api/config/claude-md", params={"at": "1900-01-01T00:00:00Z"})
    ).status_code == 404
    assert (
        await client.get(
            "/api/config/claude-md",
            params={"at": first.json()["updated_at"], "harness": "codex-cli"},
        )
    ).status_code == 404
    at = datetime.fromisoformat(first.json()["updated_at"])
    shifted = at.astimezone(datetime.fromisoformat("2026-01-01T00:00:00+02:00").tzinfo)
    assert (
        await client.get("/api/config/claude-md", params={"at": shifted.isoformat()})
    ).status_code == 200


async def test_seed_once_and_append_only_db(client: AsyncClient):
    db = await db_module.get_db()
    await db.execute("INSERT INTO skills VALUES ('instructions','instructions',1,0,NULL,'old')")
    await db.executemany(
        "INSERT INTO skill_variants VALUES (?, ?, ?)",
        [
            ("instructions", "common", "Baseline {{who}}\n"),
            ("instructions", "claude-code", json.dumps({"who": "Original"})),
        ],
    )
    await db.commit()
    await db_module._migrate(db)
    await db.commit()
    first = (await client.get("/api/config/claude-md/history")).json()
    assert len(first) == 1 and first[0]["source"] == "seed"
    assert first[0]["published_at"] is None
    rendered = await client.get(
        "/api/config/claude-md", params={"at": first[0]["recorded_at"], "harness": "claude-code"}
    )
    assert rendered.text == "Baseline Original\n"
    await db_module._migrate(db)
    await db.commit()
    assert (await client.get("/api/config/claude-md/history")).json() == first
    for sql in ("DELETE FROM document_versions", "UPDATE document_versions SET source='changed'"):
        with pytest.raises(Exception, match="append-only"):
            await db.execute(sql)
        await db.rollback()


@pytest.mark.parametrize("route", ["common", "full"])
async def test_failed_history_insert_rolls_back_document(client: AsyncClient, route):
    await publish(client)
    db = await db_module.get_db()
    await db.execute("""CREATE TRIGGER reject_history BEFORE INSERT ON document_versions
                      BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    await db.commit()
    with pytest.raises(Exception, match="injected failure"):
        if route == "common":
            await client.put("/api/config/claude-md", content="Replacement")
        else:
            await publish(client, "Replacement {{who}}", "changed")
    assert (await client.get("/api/config/claude-md")).text == "Hello {{who}}\n"
    assert len((await client.get("/api/config/claude-md/history")).json()) == 1


async def test_concurrent_publications_keep_complete_revisions(client: AsyncClient):
    await asyncio.gather(
        *(publish(client, f"Revision {i} {{{{who}}}}\n", str(i)) for i in range(6))
    )
    rows = (await client.get("/api/config/claude-md/history")).json()
    assert len(rows) == 6
    for row in rows:
        response = await client.get(
            "/api/config/claude-md", params={"at": row["published_at"], "harness": "claude-code"}
        )
        parts = response.text.strip().split()
        assert parts[1] == parts[2]


async def test_history_requires_auth(client: AsyncClient, monkeypatch):
    monkeypatch.setattr("server.config.AUTH_TOKEN", "secret")
    assert (await client.get("/api/config/claude-md/history")).status_code == 401


async def test_same_timestamp_revision_id_breaks_ties(client: AsyncClient, monkeypatch):
    monkeypatch.setattr("server.routers.skills.now", lambda: "2020-01-01T00:00:00.000000+00:00")
    await publish(client, "First {{who}}", "one")
    await publish(client, "Second {{who}}", "two")
    rows = (await client.get("/api/config/claude-md/history")).json()
    assert rows[0]["revision_id"] > rows[1]["revision_id"]
    result = await client.get(
        "/api/config/claude-md", params={"at": rows[0]["published_at"], "harness": "claude-code"}
    )
    assert result.text == "Second two"


@pytest.mark.parametrize("route", ["common", "full"])
async def test_migration_seed_precedes_new_publication(client: AsyncClient, route: str):
    db = await db_module.get_db()
    await db.execute("INSERT INTO skills VALUES ('instructions','instructions',1,0,NULL,'old')")
    await db.execute(
        "INSERT INTO skill_variants VALUES ('instructions', 'common', 'Existing baseline\\n')"
    )
    await db.commit()
    await db_module._migrate(db)
    await db.commit()
    if route == "common":
        published = await client.put("/api/config/claude-md", content="New publication\n")
    else:
        published = await publish(client, "New publication {{who}}\n", "fresh")
    assert published.status_code == 200
    history = (await client.get("/api/config/claude-md/history")).json()
    assert len(history) == 2
    assert history[0]["source"] == ("claude-md-api" if route == "common" else "skills-api")
    assert history[1]["source"] == "seed"
    assert history[1]["recorded_at"] <= history[0]["published_at"]
    historical = await client.get(
        "/api/config/claude-md", params={"at": published.json()["updated_at"]}
    )
    assert historical.status_code == 200
    assert historical.text == (
        "New publication\n" if route == "common" else "New publication {{who}}\n"
    )
