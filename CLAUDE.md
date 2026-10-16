# Hydra - Claude Code Control Plane

One server that holds memories, CLAUDE.md, and the project registry, and watches every live session. Clients sync context at SessionStart/Stop; every tool call is reported over HTTP hooks. Same bearer token, same server, two loops that don't depend on each other.

## Tech Stack

- **Server:** Python 3.13, FastAPI, aiosqlite (SQLite in WAL mode, `UNIQUE(name)` on memories)
- **Frontend:** Vanilla HTML/JS, Pico CSS (dark theme), Server-Sent Events
- **Client CLI:** `client/hydra_cli/` - stdlib `urllib`, installed via `pip install -e client/`. Hooks invoke it as `python -m hydra_cli ...` (not the `hydra` console shim) so it works without depending on a venv-bound entry point.
- **Auth:** Bearer token (fail-closed unless `HYDRA_ALLOW_NO_AUTH=1`); `secrets.compare_digest` for compare

## Running Locally

```bash
source venv/bin/activate
pip install -r requirements.txt
export HYDRA_AUTH_TOKEN=$(openssl rand -hex 32)   # or HYDRA_ALLOW_NO_AUTH=1 for dev
uvicorn server.app:app --host 127.0.0.1 --port 8400
```

The server binds to loopback by default (`HYDRA_BIND_HOST=127.0.0.1`). Reverse-proxy or VPN handles external access - see README for network options.

## Running Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
# fresh checkout or worktree: pyproject.toml declares no dependencies, so bare `uv run pytest` fails
uv venv && uv pip install -r requirements.txt -r requirements-dev.txt -e client/ && uv run pytest tests/
```

## Linting & Type Checking

```bash
ruff check server/ tests/ client/
pyright server/ tests/ client/
```

Both must pass clean. Fix issues before committing.

## Map

The file tree and the detailed patterns are not loaded at session start. Read the matching file before you touch the area.

| If your work touches | Read |
|---|---|
| any file path, router, table or test layout | `PROJECT_LAYOUT.md` (server/, client/, scripts/, static/, tests/, schema.sql) |
| session states, archive, dashboard pages, SSE broadcast | `KEY_PATTERNS.md`: Session state machine, Session archive, Dashboard pages, SSE broadcast |
| memory sync, identity, scope, upsert, guard, flow marker, legacy DBs, CLI sync, auto-register | `KEY_PATTERNS.md`: Memory identity (read before touching sync) through Flow marker, Type<->scope invariant, Upsert semantics, Legacy DBs, CLI sync, Auto-register |
| skills store/pull, multi-harness render, slash commands | `KEY_PATTERNS.md`: Skills store, Skills pull, Multi-harness, Slash-command distribution |
| policy hooks, `hooks.py` (exit-2 note), `setup.sh`, wiring | `KEY_PATTERNS.md`: Policy-hook distribution |
| token usage, pricing, `/usage` page and palette | `KEY_PATTERNS.md`: Token usage, `/usage` chart palette |
| `hydra doctor`, `/debug-hydra` | `KEY_PATTERNS.md`: Instance diagnostics |
| Remote Control URL capture, Stop hook | `KEY_PATTERNS.md`: Remote Control URL capture |
| `apply-settings`, status line, cache countdown | `KEY_PATTERNS.md`: Settings render, The status line |
| auth, body-size limits, DB access, editor deep-links | `KEY_PATTERNS.md`: Auth, Body size limits, DB access, Editor deep-links |

## Conventions

- Keep it simple. No speculative abstractions - build what the next feature needs.
- No build step for the frontend. ES modules are fine if splitting JS later.
- SQL strings go in Python (no ORM). Wrap long SQL lines at 100 chars.
- Tests use `pytest` + `httpx.AsyncClient` with isolated per-test SQLite databases.
- Run `ruff check server/ tests/ client/` and `pyright server/ tests/ client/` before considering work done.
