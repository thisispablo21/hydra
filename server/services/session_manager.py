from __future__ import annotations

import asyncio
import contextlib
import json
import re
from datetime import UTC, datetime, timedelta

from server.db import get_db
from server.models import HookEvent

# Remote Control URLs have the shape https://claude.ai/code/session_<opaque-id>.
# The ID is server-minted by Anthropic; we validate shape only, not contents.
_REMOTE_CONTROL_URL_RE = re.compile(
    r"^https://claude\.ai/code/session_[A-Za-z0-9]+$"
)

# SSE subscribers: list of asyncio.Queue that receive new events
_subscribers: list[asyncio.Queue] = []


_SUBSCRIBER_QUEUE_MAX = 1000


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue):
    with contextlib.suppress(ValueError):
        _subscribers.remove(q)


async def _broadcast(data: dict):
    # Iterate over a copy so we can drop slow subscribers mid-broadcast.
    for q in list(_subscribers):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            unsubscribe(q)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _summarize_tool_input(event: HookEvent) -> str | None:
    if not event.tool_input:
        return None
    # For file operations, show the file path
    if fp := event.tool_input.get("file_path"):
        return fp
    # For bash, show the command (truncated)
    if cmd := event.tool_input.get("command"):
        return cmd[:120]
    # For grep/glob, show the pattern
    if pat := event.tool_input.get("pattern"):
        return pat[:80]
    return None


async def handle_event(event: HookEvent, instance_id: str):
    db = await get_db()
    now = _now()
    summary = _summarize_tool_input(event)

    # Ensure session row exists (handles missed SessionStart)
    await db.execute(
        """INSERT INTO sessions
           (session_id, instance_id, status, cwd, model,
            started_at, last_event_at, files_changed)
           VALUES (?, ?, 'active', ?, ?, ?, ?, '[]')
           ON CONFLICT(session_id) DO NOTHING""",
        (event.session_id, instance_id, event.cwd, event.model, now, now),
    )

    match event.hook_event_name:
        case "SessionStart":
            # Upsert session; clear archived_at so a reactivated session
            # returns to the main dashboard view.
            await db.execute(
                """INSERT INTO sessions
                   (session_id, instance_id, status, cwd, model,
                    started_at, last_event_at, files_changed)
                   VALUES (?, ?, 'active', ?, ?, ?, ?, '[]')
                   ON CONFLICT(session_id) DO UPDATE SET
                     status='active', cwd=?, model=?, last_event_at=?,
                     archived_at=NULL""",
                (event.session_id, instance_id, event.cwd, event.model,
                 now, now,
                 event.cwd, event.model, now),
            )

        case "SessionEnd":
            # Remote Control URL dies with the CLI process; clear it so the
            # dashboard doesn't surface a dead link on the next launch.
            await db.execute(
                "UPDATE sessions SET status='ended', last_event_at=?,"
                " end_reason=?, remote_control_url=NULL WHERE session_id=?",
                (now, event.source, event.session_id),
            )

        case "UserPromptSubmit":
            await db.execute(
                "UPDATE sessions SET status='active', last_event_at=?,"
                " archived_at=NULL WHERE session_id=?",
                (now, event.session_id),
            )

        case "PostToolUse":
            updates = {"last_event_at": now, "status": "active", "archived_at": None}
            if event.tool_name:
                updates["last_tool"] = event.tool_name
            if summary:
                updates["last_tool_input_summary"] = summary

            # Track files changed for Write/Edit
            if event.tool_name in ("Write", "Edit") and event.tool_input:
                fp = event.tool_input.get("file_path", "")
                if fp:
                    rows = list(await db.execute_fetchall(
                        "SELECT files_changed FROM sessions WHERE session_id=?",
                        (event.session_id,),
                    ))
                    if rows:
                        files = json.loads(rows[0][0] or "[]")
                        if fp not in files:
                            files.append(fp)
                            updates["files_changed"] = json.dumps(files)

            set_clause = ", ".join(f"{k}=?" for k in updates)
            values = [*updates.values(), event.session_id]
            await db.execute(
                f"UPDATE sessions SET {set_clause} WHERE session_id=?",
                values,
            )

        case "Stop":
            await db.execute(
                "UPDATE sessions SET status='idle', last_event_at=? WHERE session_id=?",
                (now, event.session_id),
            )

        case "Notification":
            if event.notification_type == "idle_prompt":
                await db.execute(
                    "UPDATE sessions SET status='waiting_input',"
                    " last_event_at=? WHERE session_id=?",
                    (now, event.session_id),
                )

        case "SubagentStart" | "SubagentStop":
            await db.execute(
                "UPDATE sessions SET last_event_at=?, last_tool=? WHERE session_id=?",
                (now, f"Agent({event.agent_type or '?'})", event.session_id),
            )

    # Record the event
    await db.execute(
        """INSERT INTO events
           (session_id, instance_id, event_name, tool_name,
            tool_input_summary, received_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event.session_id, instance_id, event.hook_event_name,
         event.tool_name, summary, now),
    )
    await db.commit()

    # Broadcast to SSE subscribers
    await _broadcast({
        "session_id": event.session_id,
        "instance_id": instance_id,
        "event_name": event.hook_event_name,
        "tool_name": event.tool_name,
        "tool_input_summary": summary,
        "cwd": event.cwd,
        "received_at": now,
    })


async def get_all_sessions(archived: bool = False) -> list[dict]:
    db = await get_db()
    where = "archived_at IS NOT NULL" if archived else "archived_at IS NULL"
    rows = await db.execute_fetchall(
        f"SELECT * FROM sessions WHERE {where} ORDER BY last_event_at DESC"
    )
    sessions = []
    for row in rows:
        d = dict(row)
        d["files_changed"] = json.loads(d.get("files_changed") or "[]")
        sessions.append(d)
    return sessions


async def get_session_events(session_id: str, limit: int = 50) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    )
    return [dict(row) for row in rows]


class SessionNotFound(Exception):
    pass


class SessionStateConflict(Exception):
    pass


class InvalidRemoteControlUrl(Exception):
    pass


_ARCHIVABLE_STATES = ("ended", "idle")

# A process that dies without sending SessionEnd - closed terminal, sleep, Ctrl-C during
# a subagent - leaves its last status behind forever, and an active/waiting_input session
# can never be archived. Sweep those to ended; a resume fires SessionStart and revives it.
STALE_AFTER_DAYS = 7


async def sweep_stale_sessions() -> list[str]:
    db = await get_db()
    now = _now()
    cutoff = (datetime.fromisoformat(now) - timedelta(days=STALE_AFTER_DAYS)).isoformat()
    rows = list(await db.execute_fetchall(
        "SELECT session_id FROM sessions WHERE status != 'ended' AND last_event_at < ?",
        (cutoff,),
    ))
    swept = [row[0] for row in rows]
    if not swept:
        return swept
    await db.executemany(
        "UPDATE sessions SET status='ended', end_reason='stale', remote_control_url=NULL"
        " WHERE session_id=?",
        [(sid,) for sid in swept],
    )
    await db.commit()
    for sid in swept:
        await _broadcast({
            "session_id": sid,
            "event_name": "session_stale",
            "received_at": now,
        })
    return swept


async def archive_session(session_id: str) -> None:
    db = await get_db()
    rows = list(await db.execute_fetchall(
        "SELECT status, archived_at FROM sessions WHERE session_id=?",
        (session_id,),
    ))
    if not rows:
        raise SessionNotFound(session_id)
    status, archived_at = rows[0][0], rows[0][1]
    if archived_at is not None:
        return  # already archived - idempotent
    if status not in _ARCHIVABLE_STATES:
        raise SessionStateConflict(status)
    now = _now()
    await db.execute(
        "UPDATE sessions SET archived_at=? WHERE session_id=?",
        (now, session_id),
    )
    await db.commit()
    await _broadcast({
        "session_id": session_id,
        "event_name": "session_archived",
        "received_at": now,
    })


async def unarchive_session(session_id: str) -> None:
    db = await get_db()
    rows = list(await db.execute_fetchall(
        "SELECT archived_at FROM sessions WHERE session_id=?",
        (session_id,),
    ))
    if not rows:
        raise SessionNotFound(session_id)
    if rows[0][0] is None:
        return  # already visible - idempotent
    now = _now()
    await db.execute(
        "UPDATE sessions SET archived_at=NULL WHERE session_id=?",
        (session_id,),
    )
    await db.commit()
    await _broadcast({
        "session_id": session_id,
        "event_name": "session_unarchived",
        "received_at": now,
    })


async def set_remote_control_url(session_id: str, url: str) -> str | None:
    """Set or clear a session's Remote Control deep-link URL.

    Empty string clears; any other value must match the documented shape.
    Returns the stored value (None when cleared).
    """
    stored: str | None
    if url == "":
        stored = None
    elif _REMOTE_CONTROL_URL_RE.match(url):
        stored = url
    else:
        raise InvalidRemoteControlUrl(url)

    db = await get_db()
    rows = list(await db.execute_fetchall(
        "SELECT 1 FROM sessions WHERE session_id=?",
        (session_id,),
    ))
    if not rows:
        raise SessionNotFound(session_id)
    await db.execute(
        "UPDATE sessions SET remote_control_url=? WHERE session_id=?",
        (stored, session_id),
    )
    await db.commit()
    await _broadcast({
        "session_id": session_id,
        "event_name": "session_url_updated",
        "remote_control_url": stored,
        "received_at": _now(),
    })
    return stored


async def archive_ended_sessions() -> list[str]:
    db = await get_db()
    rows = list(await db.execute_fetchall(
        "SELECT session_id FROM sessions "
        "WHERE archived_at IS NULL AND status IN ('ended', 'idle')"
    ))
    ids = [row[0] for row in rows]
    if not ids:
        return []
    now = _now()
    placeholders = ",".join("?" * len(ids))
    await db.execute(
        f"UPDATE sessions SET archived_at=? WHERE session_id IN ({placeholders})",
        (now, *ids),
    )
    await db.commit()
    await _broadcast({
        "event_name": "session_archived_bulk",
        "session_ids": ids,
        "received_at": now,
    })
    return ids
