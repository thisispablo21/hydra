# Hydra - Coding Agent Control Plane

One server that holds your memories, instructions and skills, and watches every session - across every machine and every coding agent you use.

## Why

Two things about a coding agent hurt once you use it on more than one machine.

**Every session starts stateless.** Context you've established - preferences, project conventions, decisions already made - doesn't carry forward to the next session, let alone the next machine. You re-brief, over and over, and that friction is what keeps agents out of workflows where they'd otherwise fit.

**Every session runs in isolation.** You can't tell at a glance which session on which machine is waiting for input, what just got edited, or whether something is stuck.

Hydra is one server that solves both. A server-owned memory store, instructions document and skill set that are pulled to every machine - and every harness - when a session starts, a live dashboard that watches every session in real time, and token accounting across machines. Same server, same bearer token; the loops are independent, so observation keeps working if sync fails, and vice versa.

```
┌──────────────────────────────────────────────────────────────┐
│                    Hydra Server (24/7)                        │
│                                                               │
│     Memory store  ·  Instructions  ·  Skills  ·  Projects     │
│                            │                                  │
│                  Live dashboard (SSE)                         │
└──────────────┬──────────────────────────────┬────────────────┘
               │ SessionStart: pull           │ Stop/SessionEnd: push
               │ (memories, instructions)     │ (new memories, events)
               │                              │
       ┌───────┴──────┬───────────┬──────────┴────┐
       │              │           │               │
     Mac            WSL        SSH-1           SSH-2
  Claude Code    Claude Code   Codex CLI      Codex CLI
```

## How It Works

Two loops run continuously:

**Context loop** - `python -m hydra_cli sync` pulls each project's server memories into its local mirror (`~/.claude/projects/<dir>/memory/`). A SessionStart hook runs `python -m hydra_cli sync --pull` before Claude sees the session. The server is the only edit source; use the dashboard or `hydra memory ...`, never local mirror edits. Memories are typed: `user`/`feedback` are global (available everywhere), `project`/`reference` are pinned to the project the cwd maps to. Unregistered cwds auto-register via the server (with a stoplist for `~`, `~/Downloads`, `/tmp`, etc.) and surface in the dashboard's **Pending review** section for confirmation or deletion. The same SessionStart pass pulls server-distributed slash commands into `~/.claude/commands/` and policy hooks into `~/.claude/hooks/`, so a command or hook published once reaches every machine.

**Observation loop** - every Claude Code tool call fires an HTTP hook to `/api/hooks/event`. The server tracks session state transitions (active / idle / waiting_input / ended) and broadcasts them over Server-Sent Events to any open dashboard.

**Accounting loop** - a Claude Code Stop hook runs `python -m hydra_cli usage report`, which parses the session's transcript and its subagents' and posts per-message token counts to `/api/usage/messages`. Codex Stop and SessionEnd hooks run `python -m hydra_cli usage sweep`, which incrementally scans all local rollout files. Rows are content-keyed, so retries, sweeps and `python -m hydra_cli usage backfill` are idempotent; `/usage` prices both subscriptions at notional API-equivalent rates. `python -m hydra_cli usage reconcile codex` dry-runs a full Codex reparse against stored rows; add `--apply` to repair rows owned by the current instance without touching sweep offsets.

Same bearer token, same server, but the two loops don't depend on each other.

## Multi-harness

One server, two harnesses. Hydra serves Claude Code and OpenAI Codex CLI from the same store, over the same bearer token and the same hook wire shape. Both pull the same memories, the same instructions document and the same skills when a session starts, and both record who wrote a memory.

Shared across both:

- **Memories** - one mirror, `~/.claude/projects/<dir>/memory/`, pulled by `python -m hydra_cli sync`. Codex reaches it through its own SessionStart hook.
- **The instructions document** - one server-side body, rendered per harness and written to `~/.claude/CLAUDE.md` for Claude Code and `~/.codex/AGENTS.md` for Codex CLI.
- **Skills** - rendered per harness into `~/.claude/skills/<name>/SKILL.md`, or `~/.agents/skills/<name>/SKILL.md` plus a generated `agents/openai.yaml` for Codex.
- **The memory guard** - `python -m hydra_cli guard`, wired at `PreToolUse` on both.
- **Authorship** - every memory write records the harness, session id and model of its last writer.

The per-harness text is one common markdown body carrying `{{slot}}` markers plus a slot map per harness. The server substitutes in a single pass at render time, and a publish is refused when a harness variant leaves a marker unfilled; a harness with no variant of its own gets the common body byte-identical. No harness convention lives on the server - `implicit_invocation` travels as data and the client applies it.

### Codex CLI

`bash client/setup.sh` sets up both harnesses: `setup_claude.sh` first, then `setup_codex.sh`, which exits silently when `codex` is not on `PATH`. It wires four hooks into `~/.codex/hooks.json`: `SessionStart` runs `python -m hydra_cli codex-session-start`, `PreToolUse` runs `python -m hydra_cli guard`, and `Stop` and `SessionEnd` run `python -m hydra_cli usage sweep`. Codex does not run a new or changed hook until you trust it once - open Codex after setup and run `/hooks`. Trust is a per-entry hash in `~/.codex/config.toml` that `setup.sh` cannot pre-fill, and an untrusted hook is skipped without an error; `codex exec` prints `hook: <Event>` for each hook that fires, which is the quickest check. Deploy and restart the server before running the new `setup.sh` on any machine: the sweep sends nothing to a server that lacks the `harness` column (its `group_by=harness` probe returns 422), because that server would file Codex rows as `claude-code` for good.

### Memory writes

The local mirror is pull-only. Writes go through `hydra memory create|update|delete ... --flow <name>`, where the name is the human-gated flow that approved them; the server answers 428 without the flow marker, and the guard denies both a direct edit under the mirror and any `memory create|update|delete` command with no `--flow` in it. Set `HYDRA_FLOW_HINT` where the hooks run to name your deployment's flow in the denial text.

### Skills

Public skill sources live in `client/skills/<name>/`: `common.md` holds the frontmatter (`name`, `description`) and the body; an optional `<harness>.json` supplies that harness's slot values; an optional `skill.json` carries metadata (`enabled`, `implicit_invocation`, `instances`). A directory named `instructions` publishes the instructions document rather than a skill. `scripts/publish_skills.sh [SOURCE_DIR]` seeds the store, defaulting to `client/skills/`. `debug-hydra` is the shipped public skill.

## Setup

### Server

Run on any always-on machine: laptop, VPS, home server, Raspberry Pi.

```bash
git clone https://github.com/cesarc09/hydra.git ~/hydra && cd ~/hydra
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export HYDRA_AUTH_TOKEN=$(openssl rand -hex 32)
export HYDRA_BIND_HOST=127.0.0.1    # loopback-only; reverse-proxy terminates TLS

uvicorn server.app:app --host "$HYDRA_BIND_HOST" --port 8400
```

The server fails closed if `HYDRA_AUTH_TOKEN` is unset. For local dev without a token, set `HYDRA_ALLOW_NO_AUTH=1`. For production, run under a service manager (systemd, launchd, Docker) and put the server behind whatever reverse-proxy or TLS termination you prefer - Hydra doesn't care.

### Client (each machine that runs a coding agent)

```bash
git clone https://github.com/cesarc09/hydra.git ~/projects/hydra
export HYDRA_URL=https://your-hydra-server       # or http://localhost:8400
export HYDRA_AUTH_TOKEN=...                      # must match the server
export HYDRA_INSTANCE_ID="$(hostname)"
bash ~/projects/hydra/client/setup.sh    # Claude Code, plus Codex CLI if `codex` is on PATH
```

`setup.sh` installs `~/.claude/settings.json` with hooks pointing at `$HYDRA_URL` and installs the `hydra` CLI (`pip install -e`). It also scaffolds `~/.claude/settings.user.json` on first run - your personal layer for prefs like `effortLevel`, `attribution`, and `statusLine`. Edit a value to override, or delete a field to fall back to the template default. See [client/README.md](client/README.md) for the full layering model. Put the exports in your shell profile so hooks see them in every session.

Run `setup.sh` from a shell where `python` and `pip` resolve to the interpreter Claude Code will see at hook time - typically your base user/system Python, not a venv. The SessionStart/Stop hooks call `python -m hydra_cli`, so the package must be installed against whatever `python` is first on `PATH` when Claude Code spawns. Installing inside a venv pins hydra to that venv and the hooks silently no-op everywhere else.

For a full walkthrough covering VSCode Remote, Windows, and initial memory sync, see [ONBOARDING.md](ONBOARDING.md).

### Verify

1. Start the server.
2. Open `$HYDRA_URL/` in a browser, paste the token when prompted.
3. Start a Claude Code session on any configured machine.
4. The session appears on the dashboard within a second.

## Reaching the Server from Remote Machines

Pick whichever fits your threat model and infrastructure:

- **LAN only** - direct IP or `.local` hostname. Fine for a home setup where every client is on the same network.
- **Tailscale / WireGuard** - zero-config mesh VPN, every client reaches the server on a private address. Nothing exposed to the internet.
- **Cloudflare Tunnel** - outbound connection from the server to Cloudflare's edge. Works through CGNAT, no port forwarding. HTTPS terminated at the edge.
- **VPS with a reverse proxy** - nginx or Caddy in front of `127.0.0.1:8400`, with Let's Encrypt. Straightforward if the server has a public IP.

Regardless of network path, keep `HYDRA_BIND_HOST=127.0.0.1` and terminate TLS *somewhere* - the bearer token alone isn't a substitute for transport encryption.

## CLI Reference

Invoke as `python -m hydra_cli ...`. A `hydra` console shim is also installed by setup.sh; use it interchangeably when it's on `PATH`. The `python -m` form is the canonical one because it doesn't depend on a venv-bound entry point - the same reason hooks use it.

```
python -m hydra_cli sync [--pull] [--dry-run] [--cwd PATH]
                      # Pull server memories into the local mirror. --pull is
                      # accepted for compatibility and has no effect.
python -m hydra_cli memory list [--all|--project SLUG|--global] [--json]
                      # Defaults to this project + globals, one index line each.
                      # --json returns full rows with bodies.
python -m hydra_cli memory get ID
python -m hydra_cli memory create ... --flow <name>
python -m hydra_cli memory update ID ... --flow <name>
python -m hydra_cli memory delete ID --flow <name>
                      # Memory writes require a human-gated flow name.
python -m hydra_cli project list | get SLUG | create --slug --path | update SLUG | delete
python -m hydra_cli project prune [--apply]
                      # Propose registry cleanup: deletes only projects whose
                      # every path is contained by a confirmed project or hit by
                      # a rejection rule, and which hold no pinned memories.
                      # Dry-run unless --apply. Slug twins are reported as merge
                      # candidates, never merged.
python -m hydra_cli config get-claude-md | put-claude-md FILE
python -m hydra_cli commands pull | put NAME FILE | get NAME | list | delete NAME
                      # Slash commands. `pull` is the SessionStart hook; the
                      # rest manage what the server distributes.
python -m hydra_cli skills pull --harness claude-code|codex-cli [--adopt]
                      # Install the rendered instructions and skills for one
                      # harness. --adopt takes ownership of a pre-existing,
                      # non-identical target.
python -m hydra_cli codex-setup | codex-session-start | guard
                      # Codex hook wiring, its SessionStart entry, and the
                      # PreToolUse memory guard (both harnesses).
python -m hydra_cli hooks pull | get NAME | list | delete NAME
python -m hydra_cli hooks put NAME FILE --event EVENT [--matcher M]
                      [--runtime python|bash] [--timeout N] [--instances a,b]
                      [--disabled]
                      # Policy hooks. Each row carries the script AND its
                      # settings.json wiring; `pull` installs both and is run
                      # by setup.sh just before the settings render.
```

Global auth via `HYDRA_AUTH_TOKEN` and `HYDRA_URL` env vars.

Set `HYDRA_POLICY_HOOKS_DISABLE=1` on a machine to stop applying server-distributed policy hooks there. It empties only that layer - telemetry and memory sync keep working. (Claude Code's own `disableAllHooks` is the switch for "nothing may run at all".)

## Dashboard

Three pages, all behind the bearer token:

- **`/` - Sessions.** Live grid of session cards (active / waiting / idle / ended), grouped by status and updated via SSE.
  - **Archive** ended or idle sessions to hide them from the main view (per-card `×` or the bulk "Archive ended/idle" button). Archived sessions stay in the DB under a collapsible **Archive** section and auto-unarchive if they receive a new hook event.
  - **Filter Recent Events** by selected sessions via the chip row next to the Recent Events header. Only active / waiting / idle sessions appear as chips; selection is session-local and resets on reload.
- **`/memory` - Memory dashboard.** Browse the cross-machine memory store.
  - Global memories (`user` / `feedback`) listed separately from project-scoped memories, grouped per project with expandable rows.
  - Click a memory name to expand its body inline.
  - Per memory: **Delete**, **Move to project**, **Move to global** (pick new `user` / `feedback` type), and **Move to projects** to split a global memory across several (each copy is named `<name>-<slug>`, since names are globally unique). Re-scoping happens in place, so a memory keeps its id and its mirror files stay valid. Read-only bodies - edits still go through `python -m hydra_cli sync`.
  - Stats header: project count and memory count, split global vs project-scoped.
- **`/usage` - Token accounting.** Cost and tokens across machines and harnesses, by day / project / model / subagent.
  - Range chips (7d / 30d / 90d / all); machine and harness filters appear once a second value reports. Unknown models are counted but left unpriced, never shown as $0.

## Session State Machine

| Event | Status |
|-------|--------|
| SessionStart / UserPromptSubmit / PostToolUse | `active` |
| Stop | `idle` |
| Notification (idle_prompt) | `waiting_input` |
| SessionEnd | `ended` |

Session cards on the dashboard are grouped by status and updated live via SSE.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
ruff check server/ tests/ client/
pyright server/ tests/ client/
```

All three must pass before committing.

## Tech Stack

| Component | Choice |
|-----------|--------|
| Server | Python 3.13, FastAPI, aiosqlite |
| Database | SQLite (WAL mode, globally-unique memory names) |
| Live updates | Server-Sent Events |
| Frontend | Vanilla HTML/JS, Pico CSS (no build step) |
| Auth | Bearer token (fail-closed) |
| Client CLI | Python stdlib (`urllib`) |
