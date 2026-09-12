"""Tests for session archive/unarchive endpoints."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from server.db import get_db

pytestmark = pytest.mark.asyncio

HEADERS = {"X-Instance-Id": "test-machine"}


def hook_body(session_id: str, event_name: str, **kwargs) -> dict:
    return {"session_id": session_id, "hook_event_name": event_name, **kwargs}


async def post_event(client: AsyncClient, body: dict) -> None:
    res = await client.post("/api/hooks/event", json=body, headers=HEADERS)
    assert res.status_code == 200


async def _start_and_end(client: AsyncClient, sid: str) -> None:
    await post_event(client, hook_body(sid, "SessionStart", cwd="/tmp"))
    await post_event(client, hook_body(sid, "SessionEnd", source="user_exit"))


async def _start_and_idle(client: AsyncClient, sid: str) -> None:
    await post_event(client, hook_body(sid, "SessionStart", cwd="/tmp"))
    await post_event(client, hook_body(sid, "Stop"))


# --- Archive single session ---


async def test_archive_ended_session_hides_from_default_list(client: AsyncClient):
    await _start_and_end(client, "s1")

    res = await client.post("/api/sessions/s1/archive")
    assert res.status_code == 204

    res = await client.get("/api/sessions")
    assert res.json() == []

    res = await client.get("/api/sessions?archived=true")
    sessions = res.json()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "s1"
    assert sessions[0]["archived_at"] is not None


async def test_archive_idle_session_succeeds(client: AsyncClient):
    await _start_and_idle(client, "s1")

    res = await client.post("/api/sessions/s1/archive")
    assert res.status_code == 204

    res = await client.get("/api/sessions")
    assert res.json() == []


async def test_archive_active_session_returns_409(client: AsyncClient):
    await post_event(client, hook_body("s1", "SessionStart", cwd="/tmp"))

    res = await client.post("/api/sessions/s1/archive")
    assert res.status_code == 409

    res = await client.get("/api/sessions")
    assert len(res.json()) == 1


async def test_archive_waiting_input_returns_409(client: AsyncClient):
    await post_event(client, hook_body("s1", "SessionStart", cwd="/tmp"))
    await post_event(
        client, hook_body("s1", "Notification", notification_type="idle_prompt")
    )

    res = await client.post("/api/sessions/s1/archive")
    assert res.status_code == 409


async def test_archive_missing_session_returns_404(client: AsyncClient):
    res = await client.post("/api/sessions/nope/archive")
    assert res.status_code == 404


async def test_archive_is_idempotent(client: AsyncClient):
    await _start_and_end(client, "s1")

    assert (await client.post("/api/sessions/s1/archive")).status_code == 204
    assert (await client.post("/api/sessions/s1/archive")).status_code == 204


# --- Unarchive ---


async def test_unarchive_restores_session(client: AsyncClient):
    await _start_and_end(client, "s1")
    await client.post("/api/sessions/s1/archive")

    res = await client.post("/api/sessions/s1/unarchive")
    assert res.status_code == 204

    res = await client.get("/api/sessions")
    assert len(res.json()) == 1
    assert res.json()[0]["archived_at"] is None


async def test_unarchive_missing_session_returns_404(client: AsyncClient):
    res = await client.post("/api/sessions/nope/unarchive")
    assert res.status_code == 404


# --- Bulk archive ---


async def test_archive_ended_bulk(client: AsyncClient):
    await _start_and_end(client, "s1")
    await _start_and_idle(client, "s2")
    await post_event(client, hook_body("s3", "SessionStart", cwd="/tmp"))  # active

    res = await client.post("/api/sessions/archive-ended")
    assert res.status_code == 200
    body = res.json()
    assert body["archived"] == 2
    assert set(body["session_ids"]) == {"s1", "s2"}

    res = await client.get("/api/sessions")
    remaining = [s["session_id"] for s in res.json()]
    assert remaining == ["s3"]


async def test_archive_ended_bulk_with_nothing_to_archive(client: AsyncClient):
    await post_event(client, hook_body("s1", "SessionStart", cwd="/tmp"))

    res = await client.post("/api/sessions/archive-ended")
    assert res.status_code == 200
    assert res.json() == {"archived": 0, "session_ids": []}


# --- Auto-unarchive on new activity ---


async def test_user_prompt_unarchives_session(client: AsyncClient):
    await _start_and_idle(client, "s1")
    await client.post("/api/sessions/s1/archive")

    await post_event(client, hook_body("s1", "UserPromptSubmit"))

    res = await client.get("/api/sessions")
    sessions = res.json()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "s1"
    assert sessions[0]["archived_at"] is None
    assert sessions[0]["status"] == "active"


async def test_post_tool_use_unarchives_session(client: AsyncClient):
    await _start_and_idle(client, "s1")
    await client.post("/api/sessions/s1/archive")

    await post_event(
        client,
        hook_body("s1", "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}),
    )

    res = await client.get("/api/sessions")
    assert len(res.json()) == 1
    assert res.json()[0]["archived_at"] is None


async def test_session_start_unarchives_session(client: AsyncClient):
    await _start_and_end(client, "s1")
    await client.post("/api/sessions/s1/archive")

    await post_event(client, hook_body("s1", "SessionStart", cwd="/tmp"))

    res = await client.get("/api/sessions")
    assert len(res.json()) == 1
    assert res.json()[0]["archived_at"] is None
    assert res.json()[0]["status"] == "active"


async def test_stop_does_not_unarchive(client: AsyncClient):
    """A Stop event on an archived session should keep it archived -
    Stop alone is not a signal that the session is relevant again."""
    await _start_and_idle(client, "s1")
    await client.post("/api/sessions/s1/archive")

    await post_event(client, hook_body("s1", "Stop"))

    res = await client.get("/api/sessions")
    assert res.json() == []

    res = await client.get("/api/sessions?archived=true")
    assert len(res.json()) == 1


# --- Stale sweep ---


async def _backdate(sid: str, days: int) -> None:
    db = await get_db()
    past = (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()
    await db.execute("UPDATE sessions SET last_event_at=? WHERE session_id=?", (past, sid))
    await db.commit()


async def test_stale_active_session_is_ended_on_list(client: AsyncClient):
    await post_event(client, hook_body("s1", "SessionStart", cwd="/tmp"))
    await _backdate("s1", 8)

    res = await client.get("/api/sessions")
    (s,) = res.json()
    assert (s["status"], s["end_reason"]) == ("ended", "stale")

    res = await client.post("/api/sessions/s1/archive")
    assert res.status_code == 204


async def test_recent_active_session_is_not_swept(client: AsyncClient):
    await post_event(client, hook_body("s1", "SessionStart", cwd="/tmp"))
    await _backdate("s1", 6)

    res = await client.get("/api/sessions")
    assert res.json()[0]["status"] == "active"


async def test_resume_revives_swept_session(client: AsyncClient):
    await post_event(client, hook_body("s1", "SessionStart", cwd="/tmp"))
    await _backdate("s1", 8)
    await client.get("/api/sessions")

    await post_event(client, hook_body("s1", "SessionStart", cwd="/tmp"))
    res = await client.get("/api/sessions")
    assert res.json()[0]["status"] == "active"
