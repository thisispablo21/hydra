"""The encoded-path-traversal reject middleware (the Access-bypass fix).

Driven against a raw ASGI scope on purpose: httpx normalises `..` client-side,
so an integration request could never carry the exploit path to the app.
"""

import pytest

from server.app import RejectEncodedTraversalMiddleware

pytestmark = pytest.mark.asyncio


async def _run(raw_path: bytes) -> tuple[int, bool]:
    """Drive the middleware with a raw ASGI scope; return (status, reached_app)."""
    reached = False

    async def downstream(scope, receive, send):
        nonlocal reached
        reached = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    status = 0

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "path": raw_path.decode(errors="replace"),
        "raw_path": raw_path,
        "headers": [],
    }
    await RejectEncodedTraversalMiddleware(downstream)(scope, receive, send)
    return status, reached


async def test_encoded_slash_traversal_rejected():
    # The Access-bypass path: matches /api at Cloudflare, normalises to / here.
    status, reached = await _run(b"/api/..%2f")
    assert status == 400
    assert reached is False


async def test_encoded_dot_traversal_rejected():
    status, reached = await _run(b"/api/%2e%2e/config/claude-md")
    assert status == 400
    assert reached is False


async def test_plain_dotdot_rejected():
    status, reached = await _run(b"/api/../")
    assert status == 400
    assert reached is False


async def test_normal_paths_pass():
    for path in (b"/", b"/api/health", b"/memory", b"/usage.js"):
        status, reached = await _run(path)
        assert (status, reached) == (200, True), path
