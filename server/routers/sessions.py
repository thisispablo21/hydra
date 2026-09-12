import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from sse_starlette.sse import EventSourceResponse

from server.auth import require_auth, require_auth_sse
from server.config import EDITORS_PATH
from server.models import RemoteControlUrlUpdate
from server.services.session_manager import (
    InvalidRemoteControlUrl,
    SessionNotFound,
    SessionStateConflict,
    archive_ended_sessions,
    archive_session,
    get_all_sessions,
    get_session_events,
    set_remote_control_url,
    subscribe,
    sweep_stale_sessions,
    unarchive_session,
    unsubscribe,
)

router = APIRouter(prefix="/api", tags=["sessions"])


@router.get("/sessions", dependencies=[Depends(require_auth)])
async def list_sessions(archived: bool = False):
    await sweep_stale_sessions()
    return await get_all_sessions(archived=archived)


@router.get("/sessions/{session_id}/events", dependencies=[Depends(require_auth)])
async def session_events(session_id: str, limit: int = 50):
    return await get_session_events(session_id, limit)


@router.post(
    "/sessions/{session_id}/archive",
    status_code=204,
    dependencies=[Depends(require_auth)],
)
async def archive_session_endpoint(session_id: str):
    try:
        await archive_session(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="session not found") from None
    except SessionStateConflict as e:
        raise HTTPException(
            status_code=409,
            detail=f"cannot archive session in state '{e}'",
        ) from None
    return Response(status_code=204)


@router.post(
    "/sessions/{session_id}/unarchive",
    status_code=204,
    dependencies=[Depends(require_auth)],
)
async def unarchive_session_endpoint(session_id: str):
    try:
        await unarchive_session(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="session not found") from None
    return Response(status_code=204)


@router.post("/sessions/archive-ended", dependencies=[Depends(require_auth)])
async def archive_ended_endpoint():
    ids = await archive_ended_sessions()
    return {"archived": len(ids), "session_ids": ids}


@router.put(
    "/sessions/{session_id}/remote-control-url",
    dependencies=[Depends(require_auth)],
)
async def set_remote_control_url_endpoint(
    session_id: str, body: RemoteControlUrlUpdate
):
    try:
        stored = await set_remote_control_url(session_id, body.url)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="session not found") from None
    except InvalidRemoteControlUrl:
        raise HTTPException(
            status_code=400,
            detail="url must match https://claude.ai/code/session_<id>",
        ) from None
    return {"remote_control_url": stored}


@router.get("/editors", dependencies=[Depends(require_auth)])
async def get_editors():
    path = Path(EDITORS_PATH)
    if path.exists():
        return json.loads(path.read_text())
    return {"default": {"editor": "vscode", "type": "local"}, "instances": {}}


@router.get("/events/stream", dependencies=[Depends(require_auth_sse)])
async def event_stream():
    queue = subscribe()

    async def generate():
        try:
            while True:
                data = await queue.get()
                yield {"event": "hook_event", "data": json.dumps(data)}
        finally:
            unsubscribe(queue)

    return EventSourceResponse(generate())
