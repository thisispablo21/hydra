import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server import config
from server.db import close_db, get_db
from server.routers import config as config_router
from server.routers import hooks, memory, projects, sessions, skills, usage
from server.services.session_manager import sweep_stale_sessions

# Per-path request body caps. Pi memory is the limiting resource; `tool_input`
# is an unbounded dict otherwise.
_BODY_LIMITS: dict[str, int] = {
    "/api/hooks/event": 64 * 1024,
    "/api/config/claude-md": 1024 * 1024,
    # Usage batches are chunked at 500 messages by the client; backfill sends
    # them back to back, so this needs headroom over the default.
    "/api/usage/messages": 1024 * 1024,
}
_DEFAULT_BODY_LIMIT = 256 * 1024


class _BodyTooLarge(Exception):
    """Raised once the streamed request body exceeds its per-path cap."""


def _scope_header(scope, name: bytes) -> bytes | None:
    for key, value in scope["headers"]:
        if key == name:
            return value
    return None


async def _send_json(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """Reject request bodies over a per-path cap.

    Content-Length alone is bypassable: a chunked-encoded request carries no
    Content-Length header. So the cap is enforced on the bytes as they stream
    in through `receive`, with the Content-Length check kept only as an early
    reject before the client uploads anything.
    """

    def __init__(self, app, limits: dict[str, int], default: int):
        self.app = app
        self.limits = limits
        self.default = default

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limits.get(scope["path"], self.default)

        content_length = _scope_header(scope, b"content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                await _send_json(send, 400, "Invalid content-length")
                return
            if declared > limit:
                await _send_json(send, 413, "Request body too large")
                return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _send_json(send, 413, "Request body too large")


class RejectEncodedTraversalMiddleware:
    """Refuse percent-encoded path traversal before any routing.

    Cloudflare Access matches the *raw* request path against the `/api` Bypass
    policy, but uvicorn decodes `%2f`/`%2e` before the router sees it. So
    `/api/..%2f` slips past the Access wall guarding the dashboard, then
    normalises to `/` here and the static mount serves `index.html`
    unauthenticated. No legitimate Hydra path carries an encoded slash or dot,
    or a `..` segment, so any request whose raw path does is refused with 400.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            raw = scope.get("raw_path") or scope["path"].encode()
            lowered = raw.lower()
            if b"%2e" in lowered or b"%2f" in lowered or b".." in raw:
                await _send_json(send, 400, "Bad request path")
                return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.AUTH_TOKEN and not config.ALLOW_NO_AUTH:
        raise RuntimeError(
            "HYDRA_AUTH_TOKEN is required. Set HYDRA_ALLOW_NO_AUTH=1 for local dev."
        )
    await get_db()
    await sweep_stale_sessions()
    yield
    await close_db()


app = FastAPI(title="Hydra", lifespan=lifespan)

app.add_middleware(
    BodySizeLimitMiddleware, limits=_BODY_LIMITS, default=_DEFAULT_BODY_LIMIT
)


if config.PUBLIC_ORIGIN:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"https://{config.PUBLIC_ORIGIN}"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Hydra-Flow"],
    )


# Outermost: refuse encoded path traversal before routing (see the class above).
app.add_middleware(RejectEncodedTraversalMiddleware)


app.include_router(hooks.router)
app.include_router(sessions.router)
app.include_router(config_router.router)
app.include_router(skills.router)
app.include_router(memory.router)
app.include_router(projects.router)
app.include_router(usage.router)


@app.get("/api/health")
async def health():
    """Unauthenticated liveness + DB probe. Lets `hydra doctor` tell a down
    server apart from a wedged DB or a bad auth token (health needs no token)."""
    try:
        db = await get_db()
        await db.execute("SELECT 1")
    except Exception:
        return JSONResponse({"status": "degraded", "db": "error"}, status_code=503)
    return {"status": "ok", "db": "ok"}


# Dashboard pages and their assets are versionless: there is no build step, so
# `usage.js` changes content at a stable URL. A client holding a cached copy of
# one file against a fresh copy of another breaks the page outright (a stale
# usage.js under a new usage.html raises "startUsage is not defined"), so every
# static response revalidates. `no-cache` is not `no-store`: the ETag
# StaticFiles already sends turns the revalidation into a cheap 304.
_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/memory")
async def memory_dashboard():
    return FileResponse(
        config.BASE_DIR / "static" / "memory.html", headers=_NO_CACHE
    )


@app.get("/usage")
async def usage_dashboard():
    return FileResponse(
        config.BASE_DIR / "static" / "usage.html", headers=_NO_CACHE
    )


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles that asks clients to revalidate (see _NO_CACHE above)."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount(
    "/",
    RevalidatingStaticFiles(directory=str(config.BASE_DIR / "static"), html=True),
    name="static",
)
