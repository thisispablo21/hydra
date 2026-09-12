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

## Project Layout

```
server/
  app.py              - FastAPI entry, lifespan startup guard, body-size middleware, CORS,
                        /api/health (unauthenticated liveness+DB probe), /memory route
                        -> static/memory.html
  auth.py             - require_auth + require_auth_sse (accepts ?token= for EventSource)
  config.py           - env vars (AUTH_TOKEN, BIND_HOST, PUBLIC_ORIGIN, ALLOW_NO_AUTH, DB_PATH)
  db.py               - SQLite singleton + idempotent migrations (project_slug, archived_at)
  models.py           - Pydantic models (HookEvent, MemoryCreate/Item, ProjectItem, ...)
  routers/
    hooks.py          - POST /api/hooks/event
    sessions.py       - GET sessions/events, SSE stream (require_auth_sse), editor config,
                        POST /sessions/{id}/archive|unarchive + /sessions/archive-ended
    config.py         - GET/PUT /api/config/claude-md (empty-body guard); CRUD
                        /api/config/commands (name-validated slash-command blobs)
    skills.py         - Publish/delete and per-harness render endpoints for instructions
                        and behavioural skills
    memory.py         - CRUD /api/memory; write-flow tripwire; upsert on name
                        (globally unique; 409 on cross-scope name without rescope)
    projects.py       - CRUD /api/projects + auto-register + confirm endpoints
    usage.py          - POST /api/usage/messages (INSERT OR IGNORE on message_id),
                        GET /api/usage/summary?group_by=day|model|project|instance|agent
                        (+ since / until / instance filters)
  pricing.py          - Per-model rate table; prices grouped rows at QUERY time.
                        Unknown model -> cost None + unpriced_messages, never 0
  services/
    session_manager.py - State machine, bounded SSE broadcast, DB writes, archive ops
    skills.py          - Marker discovery, one-pass slot rendering, variant validation,
                         shared skills-store write lock
    slug.py            - Slug normalization + stoplist for auto-registered projects
client/
  hydra_cli/
    __main__.py            - CLI dispatch (memory, project, config, commands, hooks, skills, guard,
                              sync, usage, doctor, Codex hooks, capture-remote-url,
                              apply-settings)
    api.py                 - Stdlib urllib + bearer token + custom headers
    author.py              - Last-writer authorship for `memory create|update`: harness + session
                              from the env, model from the newest transcript record or --model
    guard.py               - PreToolUse guard for direct mirror edits and unmarked CLI writes
    sync.py                - Memory sync, keyed on the server row id (stamped into each
                              mirror file's frontmatter). Frontmatter parse, collision-free
                              filename assignment, tombstones, MEMORY.md regen
    commands.py            - `commands pull`: fetch the server command map, write
                              ~/.claude/commands/<name>.md, state-file-scoped prune
    hooks.py               - `hooks pull`: fetch the server hook map, install
                              ~/.claude/hooks/<name>.<ext>, render the wiring layer
                              ~/.claude/settings.hooks.json, state-file-scoped prune
    skills.py              - `skills pull`: install rendered instructions and skill packages
                              for Claude Code or Codex, with per-harness managed-file state
    codex.py               - Codex SessionStart adapter and idempotent hooks.json writer
    paths.py               - path shape + containment + rejection rules; a deliberate
                              duplicate of server/services/slug.py (the client cannot
                              import server.*), kept honest by a shared test corpus
    prune.py               - `project prune`: human-gated registry cleanup. --dry-run by
                              default; reports slug twins as merge candidates, never merges
    usage.py               - Stop/SessionEnd entry: parses transcript usage, per-session
                              byte offsets in ~/.claude/.hydra-usage/, `usage backfill`
    usage_codex.py         - Codex rollout parser + sweep, shared atomic offset state,
                              root-session batching and server capability handshake
    remote.py              - Stop-hook entry: scans transcript for the bridge record, PUTs URL
    apply_settings.py      - 4-way merge for ~/.claude/settings.json (hydra hooks →
                              server policy hooks → template defaults → user overrides)
  settings.json            - Hydra hooks template (__HYDRA_URL__ / __HYDRA_REPO_PATH__ placeholders)
  settings.user.template.json - User-pref defaults (effortLevel, attribution, statusLine, …);
                                scaffolded to ~/.claude/settings.user.json on first run
  hydra_statusline.sh      - Status-line launcher; installed to ~/.claude/hydra_statusline.sh
                              (Hydra-owned, overwritten every run; a user's own
                              ~/.claude/statusline.sh is never touched)
  hydra_statusline.py      - Status-line renderer: context bar, 250k SPLIT flag, cache countdown
  skills/                  - Repo-authored public skills, seeded into the skills store
    debug-hydra/common.md  - Diagnose Hydra health, anomalies, and fixes via `hydra doctor`
  setup.sh                 - Shared CLI install gate; dispatches both harness setup scripts
  setup_claude.sh          - Pulls Claude content, renders settings, installs the status line
  setup_codex.sh           - Wires the Codex SessionStart hook when Codex is installed
scripts/
  publish_skills.sh     - Entry point for seeding client/skills/ into the skills store
  publish_skills.py     - Stdlib-only skill source validation and publisher; sends the
                          hydra-cli User-Agent (Cloudflare answers 403 error 1010 without one)
static/
  index.html, app.js   - Sessions dashboard (/); archive, Recent Events chip filter
  memory.html, memory.js - Memory dashboard (/memory); browse, delete, copy, move,
                            pending-review queue for auto-registered projects
  usage.html, usage.js, usage.css - Usage dashboard (/usage); KPI row, daily cost
                            column chart (inline SVG, no charting lib), ranked tables,
                            token-vs-cost composition bars. Categorical slots are
                            validated - see Key Patterns before recolouring. The
                            machine filter hides itself below 2 machines, and its
                            list is fetched UNSCOPED so picking one never hides
                            the rest
  utils.js             - Shared apiFetch + token handling, escHtml (loaded before page JS)
  style.css            - Shared styles (no build step, bearer-token prompt)
tests/
  conftest.py         - Per-test isolated SQLite + test client; sets ALLOW_NO_AUTH=True
  test_hooks.py       - Hook ingestion + state machine
  test_config.py      - CLAUDE.md endpoint
  test_config_skills.py - Skills publish/delete, validation, auth, and render endpoints
  test_publish_skills.py - Repo-authored skill validation and publishing
  test_memory.py      - Memory CRUD + unique-name upsert/409/rescope + unpin + scoping
  test_migrations.py  - Legacy partial-index DB → UNIQUE(name) (twin collapse, rename,
                        idempotency)
  test_projects.py    - Project CRUD
  test_sync.py        - CLI pull sync, parsing, pruning, provenance, canonical
                        filenames, empty-server safety
  test_commands.py    - /api/config/commands endpoints (CRUD, name validation, auth)
  test_commands_pull.py - `commands pull` write + state-file-scoped prune
  test_skills_pull.py - Harness-specific skill writes, ownership safety, filters, and prune
  test_config_hooks.py - /api/config/hooks endpoints (CRUD, name + metadata validation,
                        name ordering, auth). Named to avoid test_hooks.py (ingestion)
  test_skills_render.py - Skills marker grammar, one-pass render, and validation
  test_slug_stoplist.py - path shape / containment / rejection corpus, as module-level
                        case lists so the client copy can run the identical cases
  test_client_paths.py - the same corpus against client/hydra_cli/paths.py (drift guard)
  test_project_prune.py - prune bucketing, mixed/pathless keeps, report-only merges,
                        and the destructive --apply path
  test_hooks_pull.py  - `hooks pull`: install + wire, wire-only-what-is-on-disk, instance
                        and enabled filters, compile-failure retain, empty-server safety
  test_health.py      - /api/health probe (200 + DB ok; reachable without auth)
  test_doctor.py      - `hydra doctor` report (stats aggregation + anomaly checks, mocked api)
  test_capture_remote_url.py - `capture-remote-url`: both bridge shapes, last-record-wins,
                        disconnect vs drift, the three-way PUT split, + a corpus canary
                        whose denominator is computed independently of the parser
  fixtures/           - REDACTED real transcript lines (synthetic ids) seeding the above
  test_session_archive.py - Archive endpoints + auto-unarchive on new activity
  test_session_cwd.py - Hook + CLI cwd pinning: CLAUDE_PROJECT_DIR beats a drifted
                        $PWD, fallback retained (parses the shipped hook commands)
  test_codex_session_start.py - Codex hook JSON/context output and hooks.json setup
  test_guard.py       - Cross-harness memory-write guard and fail-open behavior
  test_statusline.py  - Status-line render: bar, 250k SPLIT gate, cache countdown
  test_startup.py     - Fail-closed startup guard
  test_usage.py       - /api/usage ingest idempotence (replay, cross-session id, no FK)
                        + harness grouping/filter, per-model pricing, unpriced models
  test_usage_codex.py - Codex cumulative parser, parent-model inheritance, sweep state,
                        handshake, batching and redacted rollout corpus canary
  test_usage_report.py - `usage report`: record→message dedupe, subagent discovery,
                        symlinked workflow dirs, offsets (failure, partial line, truncation)
schema.sql            - DDL; sessions.archived_at, memories project_slug FK + UNIQUE(name)
                        (inline; db._migrate installs it on legacy DBs - see Key Patterns),
                        skills + skill_variants,
                        config_commands (server-distributed slash commands),
                        config_hooks (server-distributed policy hooks: script + wiring),
                        usage_messages (per-API-message token counts, PK message_id)
```

## Key Patterns

- **Session state machine:** SessionStart/UserPromptSubmit/PostToolUse → `active`, Stop → `idle`, Notification(idle_prompt) → `waiting_input`, SessionEnd → `ended`
- **Session archive:** only `ended` / `idle` can be archived. SessionStart / UserPromptSubmit / PostToolUse clear `archived_at` - archived sessions auto-surface when they wake up. `Stop` alone does NOT unarchive. A process that dies without a SessionEnd keeps its last status forever and, if `active`/`waiting_input`, can never be archived, so `sweep_stale_sessions` ends every non-ended session with no event for `STALE_AFTER_DAYS` (7) as `end_reason='stale'`, on startup and on every `GET /api/sessions`; a resume fires SessionStart and revives it.
- **Dashboard pages:** `/` (sessions) and `/memory` (memory) are separate HTML/JS entry points. Shared helpers (`apiFetch`, `ensureToken`, `escHtml`) live in `static/utils.js`; both pages load it before their own script. No bundler.
- **Memory identity = the row id (read this before touching sync).** A memory is its server row id. Pull stamps `id:` and `updated_at:` provenance into each mirror file's frontmatter. Server-deleted and re-scoped rows are tombstones for that project mirror and are pruned on pull, never re-inserted from disk.
  - **An empty server is never authority to delete.** A wrong `HYDRA_URL`, a fresh DB and a half-restored backup all look exactly like "everything was deleted", and the mirror may be the only copy left. `run_sync` checks the whole corpus (`fetch_whole_corpus()`, fetched once and lazily), not the project's slice, so a project with no memories of its own still prunes correctly. Prune also keeps unparseable files and id-less files whose names are absent from the server. With an authoritative server, these strays stay on disk but are omitted from `MEMORY.md`; without authority, the index is rebuilt from disk.
- **Memory scope:** type=user/feedback -> global (project_slug=NULL); type=project/reference -> pinned to a registered project. The dashboard and `hydra memory ...` are the edit paths; sync only mirrors server state.
- **Memory authorship:** `author_harness`, `author_session_id` and `author_model` describe the last writer. POST and PUT replace all three; absent means null. The migration backfills only newly-added `author_harness` columns with `claude-code`. The CLI gets harness and session from its environment and model from `--model` or the newest matching transcript record.
- **Memory guard:** `python -m hydra_cli guard` denies direct writes under Claude's memory mirrors and `memory create|update|delete` commands without `--flow`. For `Bash` it also denies a mirror path as an operand of anything outside a small read allowlist or as a `>`/`>>` target of anything at all (the heredoc hole), with `~` expanded against the payload's home; a path inside a quoted program string is one token and invisible - a tripwire, not a sandbox. Both Claude Code and Codex wire it at `PreToolUse`. It allows reads and marked human-gated writes, and malformed or unexpected payloads fail open so a broken guard never blocks every tool call.
- **Flow marker:** POST/PUT/DELETE `/api/memory` require `X-Hydra-Flow` or return 428. It is a tripwire, not authorisation; the CLI exposes `--flow`, the UI sends constant `dashboard`, and CORS allows the header. The user-settings template ships `HYDRA_FLOW_HINT`, which the guard appends to its denial text to name this deployment's accepted flows.
- **Type<->scope invariant, enforced in BOTH directions** (`_type_for_scope` in `routers/memory.py`, on upsert *and* update). Pinned + a global type -> coerced to `project` (this is what auto-scopes the dashboard's Move-to-project). Global + a project-scoped type -> **422**, because there is no way to guess user vs feedback; the caller has to say. So `hydra memory update <id> --global` requires `--type user|feedback`, and the dashboard's Move-to-Global asks for one.
- **Skills store:** `skills` holds instructions and behavioural-skill metadata; `skill_variants` holds one common markdown body plus optional harness slot maps. Markers are exactly `{{name}}` for lowercase identifier names, rendered in one pass. Validation covers only variants present, and a missing harness variant leaves common byte-identical. Full publishes run under one shared lock, and the render reads take the same lock: on the shared connection an unlocked SELECT can land between a publish's `DELETE` and its commit, and a response missing one skill makes the client prune that skill's files. `/api/config/claude-md` round-trips raw `instructions/common`, while SessionStart pulls the rendered harness map. Migration copies a legacy blob only when no instructions row exists, then always drops `claude_md`.
- **Skills pull:** `skills pull --harness claude-code|codex-cli` fetches the rendered harness map once, applies `enabled` and `instances` locally, and installs instructions plus skills with atomic per-file writes. Per-harness state in `~/.claude/.hydra-skills-<harness>.json` scopes prune to owned skill files; an empty server prunes nothing, instructions are never pruned, unmanaged conflicts require `--adopt`, and symlinks are always refused. Claude gets `disable-model-invocation` frontmatter for explicit-only skills; the server's `implicit_invocation` overwrites any `disable-model-invocation` the body already carries, so a rendered SKILL.md pasted back into `common.md` cannot invert the policy. Codex gets verbatim SKILL.md plus generated `agents/openai.yaml`.
- **Multi-harness:** the server is harness-agnostic - render is slot substitution over one common body (`server/services/skills.py`), and `implicit_invocation` is returned as data on the render-all response for the client to apply at install time. Harness conventions live in three client modules: `client/hydra_cli/skills.py` (install paths, Claude frontmatter, generated `agents/openai.yaml`), `client/hydra_cli/codex.py` (hook wiring and SessionStart), and `client/hydra_cli/usage_codex.py` (Codex rollout accounting). Both harnesses share one hook wire shape - the same stdin JSON (`session_id`, `transcript_path`, `cwd`, `hook_event_name`) and the same `permissionDecision: deny` reply - which is why `guard.py` needs no harness detection and only the matchers differ (`Write|Edit|NotebookEdit|Bash` for Claude, `Bash|apply_patch` for Codex). Two facts that bit us: every wiring says bare `python`, never `python3` and never an absolute path, and Codex skips a new or changed hook until the user trusts it via `/hooks`, so rewriting a wired command string costs a manual re-trust on every machine. Changing a `matcher` in `_HOOKS` needs a migration: `_wire_event` rewrites a stale entry in place inside its old group, so the old matcher survives on every provisioned machine. Policy-hook conventions now also live in `client/hydra_cli/hooks.py`: hooks share one script body and carry per-harness event/matcher/timeout wiring.
- **Slash-command distribution:** `config_commands` is a `name -> content` blob table; `GET /api/config/commands` returns the whole `{name: content}` map in one round trip, plus per-name GET/PUT/DELETE. Names are validated server-side to `^[A-Za-z0-9][A-Za-z0-9_-]*$`. The SessionStart `commands pull` hook writes each into `~/.claude/commands/<name>.md` **verbatim** - deliberately NOT via `sync.py`'s `_base_slug`, which would rename `code-review` -> `code_review` and break the command - and prunes via a managed-names state file (`~/.claude/.hydra-commands.json`) so it only ever deletes files it wrote. It stays for private, unmigrated commands; the public `debug-hydra` is a skill in `client/skills/`, seeded with `scripts/publish_skills.sh`. Once a command is migrated, `hydra commands delete <name>` on the server lets the next pull prune its old file.
- **Policy-hook distribution (read the exit-2 note before touching `hooks.py`).** `config_hooks` carries a hook's **script body and its settings.json wiring in one row**, and they must never travel separately. `python <missing>.py` exits **2**, and exit 2 on `PreToolUse` is the *blocking* code - so wiring that reaches a machine ahead of its script converts a fail-open guard into a hard deny of every matching tool call. `run_pull` therefore emits wiring **only for a name whose file is on disk after the write phase**; that check is the whole safety story, not the compile check. Corollaries:
  - **Per-harness wiring lives in the `wiring` JSON column.** The legacy `event`/`matcher`/`timeout` columns mirror only the `claude-code` entry and stay for one fleet-update release; drop them only after every client uses `/api/config/hooks/render/{harness}`. A row without `claude-code` wiring is hidden from the harness-less `GET /api/config/hooks`. A `PUT` replaces the whole `wiring` map: the flag form, or a directory without `codex-cli.json`, drops the row's Codex entry.
  - **Wiring says `python`, never `python3`, and never an absolute path.** Hook commands run in shell form - `sh -c` on macOS/Linux, **Git Bash on Windows**, PowerShell only if Git Bash is absent - and Windows has no `python3` on PATH (the python.org installer ships `python.exe` and `py.exe`; the `python3.exe` that resolves there is the Microsoft Store alias stub). `python3` wiring therefore installed all four policy hooks on Windows and ran none of them, silently, for weeks. Bare `python` is the same interpreter contract `setup.sh` and `client/settings.json` already depend on, and a bare name keeps the layer stable where an absolute `sys.executable` would rewrite `settings.hooks.json` on every venv switch. `run_pull` warns to stderr when a wired runtime's interpreter is not on PATH, because that failure is otherwise invisible: it exits 127, not the blocking 2. `$HOME` in the script path is fine - sh, Git Bash and PowerShell all expand it. Policy script paths use `$HOME`, except an explicitly non-default `CODEX_HOME`, whose script path is absolute in the command; the interpreter stays bare.
  - **A syntax-broken script keeps the previous file *and* its wiring.** Python content is `compile()`-checked before it is written; on `SyntaxError` the last-good script stays installed and stays wired, because running the previous version beats running nothing for a fail-open hook. A broken script with *no* previous version gets no wiring at all.
  - **Prune is scoped by a state file of FILENAMES** (`~/.claude/.hydra-hooks.json`), not names and never a glob - so hand-authored hooks in `~/.claude/hooks/` survive forever, and a hook whose `runtime` changes has its old suffix pruned correctly. **An empty server is never authority to delete** (same rule as `sync.py`): a wrong `HYDRA_URL` and a fresh DB look exactly like "every hook was deleted", so a 0-hook response prunes nothing. The wiring layer still empties, so the retained scripts are inert. Prune is deferred by one pull: a dropped script loses wiring now and is deleted only on the next non-empty pull, covering the Claude window between `hooks pull` and a successful `apply-settings` render and a Codex session still holding the previous `hooks.json`. Codex ownership uses `$CODEX_HOME/.hydra-hooks.json` (default `~/.codex/.hydra-hooks.json`). Symlinked and non-regular targets are always refused; unmanaged regular files require `--adopt` to take ownership. Refused targets are neither wired, managed nor pruned.
  - **`setup.sh` runs `hooks pull` immediately *before* `apply-settings`**, so the generated layer is fresh when the single renderer reads it and the puller never has to re-enter the merge. Hooks hot-reload from a watched settings file (verified 2026-07-27: a hook added mid-session fired on the next tool call), so the re-render takes effect in the same session - no one-session lag.
  - **Scope filters are client-side.** `enabled` is the fleet-wide off switch; `instances` (JSON array, NULL = everywhere) is matched against `HYDRA_INSTANCE_ID` by the *client*, so `GET /api/config/hooks` keeps returning the whole fleet's config to any machine. `ORDER BY name` on that query - SQLite promises no order without it, and an unstable one rewrites `settings.json` every pull. Per-harness render endpoints also use `ORDER BY name` and leave scope filtering to the client.
  - **Codex ownership comes only from the state file.** A group is owned when it has one command entry referencing a managed or just-dropped filename. Owned groups retain their list slot; new ones append and removed ones disappear. Hydra `_HOOKS`, multi-entry groups and commands for unrecorded filenames remain structurally unchanged. A removal shifts later groups and costs one `t` in `/hooks` per machine to re-trust them. `_wire_event` never touches policy groups because their commands do not carry the `python -m hydra_cli` prefix.
  - **`HYDRA_POLICY_HOOKS_DISABLE` empties the wiring layer and nothing else.** Hydra's telemetry `http` hooks and the `sync` / `commands pull` / `capture-remote-url` lines live in `client/settings.json`, a layer the puller never writes, so observability survives a machine switching its policy hooks off. Claude Code's own `disableAllHooks` is the wider blast radius when nothing may run. Codex removes only state-owned policy groups; scripts and ownership state remain for re-enable.
  - **Hook sources live outside this repo**, like private slash commands - Hydra ships the mechanism and no `client/hooks/` content, seeded with `hydra hooks put`.
- **Token usage: stable row identity is the correctness boundary; offsets are only an optimisation.** `usage_messages.message_id` is the PK and ingest is `INSERT OR IGNORE`, which makes Stop-hook retries, `usage backfill` re-runs, Codex sweeps and resumed sessions safe. Claude rows use `message.id`; Codex rows use a deterministic content key. The client's byte offsets only decide what is *sent* and may be lost or stale without corrupting anything. Traps that shaped this, all measured on a 719-file / 41k-record Claude corpus:
  - **One API message writes N assistant records, each repeating the same usage** (41,036 records → 17,726 messages; 2.55x output inflation on one session). Dedupe on `message.id`, first seen wins.
  - **Subagent usage is not in the main transcript** - it lives in `<dir>/<session_id>/subagents/**`, self-describing via `attributionAgent`. `transcript_files()` uses `rglob`, which does **not** follow symlinks, and that is deliberate: Claude Code aliases a workflow's subagent dir into sessions that consume it (`A/…/wf_x -> B/…/wf_x`), and following the link would attribute one workflow's agents to whichever session scanned first. Every real file is reachable without symlinks, so nothing is lost.
  - **`message.id` repeats across transcript files** after a resume or fork (124 shared ids between two real sessions) - per-session dedupe is not enough, hence the global PK.
  - **`toolUseResult.totalTokens` is the final message's counters summed, not a run total** (190k reported vs 18.5M actually spent). Use it for attribution, never spend.
  - **`usage_messages.session_id` has no FK** (unlike `events.session_id`): backfill imports sessions the server never saw, and `PRAGMA foreign_keys=ON` would reject exactly those.
  - **Cache writes are split 5m/1h** (1.25x vs 2x base input; Claude Code writes 1h). The TTL buckets and `cache_creation_input_tokens` disagree ~15 times in 41k records in both directions: buckets win when non-zero (they match `iterations`), and a positive shortfall against the reported total is added to the 5m bucket rather than dropped.
  - **Cost is priced at query time in `server/pricing.py`, never stored**, so a rate fix corrects history. Each model maps to a `Rate` with its own cache-read multiplier; write multipliers remain shared. An unknown model yields `cost_usd: None` + `unpriced_messages`, **never 0** - a silent $0 for a new model is the one failure that makes the dashboard quietly wrong. No `[1m]` dimension is needed: every current model serves 1M context at standard rates. `cost_components` splits a group's cost by token kind server-side rather than in JS, so the rate table stays the single source of truth.
  - **Codex usage is cumulative and must be differenced.** In the measured rollout corpus, 121 events re-emitted a prior cumulative total with non-zero `last_token_usage`, while every reset started clean. The parser therefore diffs cumulative counters, uses `last_token_usage` only when a counter decreases, drops zero deltas, and keys rows as `(thread, timestamp, cumulative total)`. The shared sweep state stores one byte offset per file, skips unchanged files by size without opening them, and safely replays after lost state through `INSERT OR IGNORE`. Guardian review rows inherit the immediate parent's model at the child's start time. Stop requests 10 s; SessionEnd requests Codex's 3 s clamp and remains best-effort.
- **`/usage` chart palette is validated, not chosen by eye.** The four categorical slots in `static/usage.css` (`--series-1..4`) are the dark steps of a validated palette in a fixed order, measured against this page's real `#2C3233` surface: worst adjacent CVD ΔE **9.4**, normal-vision **26.5**, all ≥3:1 contrast. Reordering or substituting them re-runs those gates - a hand-picked warm set failed both the dark lightness band and the chroma floor (two of four would have read grey). Slots follow the entity, never its rank, so a filter never repaints the survivors. Everything else on the page is magnitude, which is why the ranked bars are deliberately **one** hue and carry meaning by length. Three defects only a render caught: a scaled `viewBox` distorts label text (render at measured pixel size), `max/4` ticks read `$47.21` (snap to 1/2/5×10ⁿ), and columns need a **band** scale or the first one sits on the axis labels.
- **Instance diagnostics (`hydra doctor` + `/debug-hydra`):** the gathering lives in the CLI, not the slash command. `hydra doctor` probes `/api/health` (unauthenticated, catches `URLError` -> server DOWN), then an authed call (200/401 -> auth state), then aggregates stats, reports Remote Control capture health, and checks corpus invariants plus id-less or unparseable local memory strays. It prints a labeled report and **exits 0 with status in the text** so a wrapper never loses output - run it standalone for a zero-token health check. `/debug-hydra` just runs it and spends tokens on interpretation: a slash command earns its round-trip only when the LLM adds judgment, so raw stats belong in the CLI, never a relay-only command. `/api/health` must be deployed (server restart) before doctor reports `server: UP`; a stale server 404s as `DEGRADED`.
- **Upsert semantics:** memory names are **globally unique, scope-independent** - one name = one memory (`UNIQUE(name)`). `POST /api/memory` upserts on name; a POST whose name already exists in a *different* scope is **409** unless it passes `rescope: true`, preventing accidental scope changes. `PUT /api/memory/{id}` uses `model_dump(exclude_unset=True)`, so `{"project_slug": null}` **unpins** - dropping every None instead would make an unpin unexpressible, which is what forced re-scopes through delete + re-create in the first place.
- **Legacy DBs (`_ensure_unique_memory_names` in `db.py`):** pre-existing DBs used two *partial* unique indexes, so one name could exist twice (once global, once pinned) - the shape the duplicate bug lived in. The migration collapses exact twins (a global row byte-identical to a pinned one is a stale-mirror re-insert; the pinned row wins), **renames** any remaining duplicate rather than deleting it, then swaps in `UNIQUE(name)`. The unique index is inline in `schema.sql` for fresh DBs but installed by `_migrate` for existing ones, because `get_db()` runs `schema.sql` *before* `_migrate` and a bare `CREATE UNIQUE INDEX` would abort startup while duplicates still exist.
- **SSE broadcast:** `session_manager._subscribers` is a list of `asyncio.Queue(maxsize=1000)`. Slow consumers are dropped on `QueueFull` rather than blocking the broadcast.
- **Auth:** Fail-closed when `HYDRA_AUTH_TOKEN` is empty unless `HYDRA_ALLOW_NO_AUTH=1`. `require_auth` reads the `Authorization` header; `require_auth_sse` also accepts `?token=` (EventSource can't set headers).
- **Body size limits:** Per-path in `server/app.py` (64KB hooks, 1MB CLAUDE.md, 256KB default); 413 on overrun.
- **DB access:** `get_db()` is a module-level singleton. Tests patch `server.db._db` to an isolated connection via the `client` fixture.
- **CLI sync:** `python -m hydra_cli sync --cwd <path>` maps cwd -> project slug via `/api/projects`, then pulls globals + project-pinned memories to `~/.claude/projects/<dir-slug>/memory/`. `memory_dir_for_cwd` uses Claude Code's exact encoding: every non-alphanumeric char (including `_` and `.`) becomes `-`. **Filenames are assigned over the whole server set at once** (`canonical_filenames`), because the base slug is not injective. Lowest id keeps `<slug>.md`; later claimants get `<slug>-<id>.md`. **Prune-on-pull** removes non-canonical server tombstones only for registered cwds against a non-empty corpus. Unparseable files and id-less files whose names are absent from the server remain on disk. `MEMORY.md` lists only canonical server files when authoritative, but falls back to disk state when the corpus is empty.
- **Auto-register:** unregistered cwds POST to `/api/projects/auto-register` on SessionStart's pull. The server resolves exact paths (this instance, then any instance, compared by path shape), then containment against **confirmed** projects - a contained cwd resolves to its ancestor and writes no path row - and only then derives a slug and applies the rejection rules. Auto-flagged entries (`projects.auto_registered_at`, `project_paths.auto_registered_at`) surface in the `/memory` dashboard's **Pending review** section with Confirm/Delete actions.
- **Editor deep-links:** `editors.json` maps instance_id → editor URI scheme (vscode://, cursor://, wsl, ssh-remote, jetbrains).
- **Remote Control URL capture (the shape moved once; assume it will again).** The Stop hook runs `python -m hydra_cli capture-remote-url`, which scans `transcript_path` and PUTs the latest URL to `/api/sessions/{id}/remote-control-url`. Claude Code has written the bridge in two shapes: the legacy `{type:"system",subtype:"bridge_status",url:...}` event (2.1.118 - 2.1.240) and `{type:"bridge-session",bridgeSessionId:"cse_..."}` (2.1.142 onward, and the **only** shape since 2.1.250, when Remote Control became on by default and the legacy event stopped being emitted). Both are read as **one rule - last bridge record in file order wins** - not as a preference ordering, because file order is what stops a disconnect from resurrecting a dead URL. The legacy branch stays only until every instance is known to be past 2.1.250; Hydra records no CC version per session, which is why that is currently unanswerable.
  - **The URL is derived, and the derivation is behind a feature flag.** `cse_<id>` -> `https://claude.ai/code/session_<id>`, mirroring CC's own `toCompatSessionId`; verified at 1825/1825 records across the 64 transcripts carrying both shapes. CC gates that swap on `tengu_bridge_repl_v2_cse_shim_enabled` (default on). If it is ever turned off, CC builds `/code/cse_...` while we keep deriving `/code/session_...` - a URL the server accepts and that points nowhere, with **no local signal**. `hydra doctor` prints the CC version so a drift report can name it. The legacy branch validates CC's own URL rather than trusting it, which catches the flip on older CLIs.
  - **Three outcomes, never two.** URL found -> PUT it; **no bridge records at all -> PUT nothing**; records but nothing derivable -> report drift. Collapsing the middle case into a PUT of `""` would make every VS Code session wipe its own manually-pasted URL on every Stop, since VS Code transcripts contain no bridge records - a worse regression than the one it fixes. A disconnect tombstone (`bridgeSessionId:""`) clears the running value so no dead URL is sent, but does not clear server-side (`SessionEnd` already does) and is **not** reported as drift - an orderly shutdown must not look like a broken parser, which is a third state, not a second.
  - **Failure is loud by exit code, because stderr alone is invisible.** On exit 0 Claude Code files a hook's stderr into the transcript only; the "Stop hook error" notification fires solely for a non-zero exit. So drift and a server 400 `sys.exit(1)` - non-blocking, no model turn - at most **once per session** (marker in `~/.claude/.hydra-remote/`, created only when something is actually broken). **Never exit 2 from a Stop hook:** it blocks stopping and re-injects stderr as a synthetic user turn, looping the model. The wiring is `...; [ $? = 1 ] && exit 1; exit 0`, which passes 1 through and swallows everything else - including an accidental argparse `2`. Offline machines (`api._request` does not catch `URLError`) and the 404 SessionStart race stay silent.
  - Filter on the JSON shape, not substring grep - user/assistant text quoting these names can otherwise contaminate the scan. The old tests missed all of this because they built every input from a hand-written `bridge_status` fixture; the corpus canary in `test_capture_remote_url.py` computes its denominator with an **independent** literal check, or a blind parser would report no records and pass vacuously.
- **Settings render (4-way merge):** `~/.claude/settings.json` is composed by `python -m hydra_cli apply-settings` (called from `setup.sh`) from four layers, in priority order: (1) `client/settings.json` - Hydra hooks template, (2) `~/.claude/settings.hooks.json` - the server policy-hook layer generated by `hooks pull` (absent is normal; malformed degrades to "no server hooks" rather than costing the user their settings file), (3) `client/settings.user.template.json` - shipped defaults (effortLevel, statusLine, attribution, …), (4) `~/.claude/settings.user.json` - user overrides. Hooks per event concatenate (Hydra first, then server hooks, user appended); other top-level keys: later layers win. The generated layer carries **only** a `hooks` key for that reason - any other key there would outrank the shipped defaults. A third migration strips user-file wiring for a hook the server now distributes (matched on the full `.claude/hooks/<filename>` path, so hand-authored hooks are untouched): `merge` concatenates rather than dedupes, so a hook left in both layers fires twice. The user file is scaffolded as a copy of the template on first run so users see all available knobs. **Deleting** a key in the user file falls back to the template default - that's the sanctioned way to opt out of a default (e.g. `statusLine`) without losing it for everyone else. **Migration:** because scaffolded user files pin old defaults forever, `apply-settings` migrates old-format user files in place: a stale `effortLevel: "max"` (legacy of the removed CLAUDE_CODE_EFFORT_LEVEL env promotion, which couldn't be overridden in-session) is dropped, and a top-level `defaultMode` moves inside `permissions` where Claude Code expects it. A fourth migration drops a `statusLine` block byte-equal to the pre-`refreshInterval` scaffold: the user layer wins outright on that key, so an untouched copy would pin every machine to the day it was scaffolded and no later template change to it could ever land.
- **The status line is a managed pair, not a scaffold - and it is namespaced.** `client/hydra_statusline.sh` (launcher) and `client/hydra_statusline.py` (renderer) install to `~/.claude/hydra_statusline.{sh,py}` and are **overwritten unconditionally** on every `setup.sh` run, which is what makes a fix propagate at all - SessionStart runs `git pull --ff-only && bash client/setup.sh` on every machine, so a commit reaches the fleet on the next session - though the chain is `&&`-gated and silenced, so an upstream-less, diverged or dirty checkout fails the pull and skips `setup.sh` entirely, updating nothing and saying nothing. The `hydra_` prefix is the whole point: `~/.claude/statusline.sh` is now a path Hydra never reads or writes, so a user can keep their own script there and point `statusLine.command` at it in `settings.user.json`. Nothing is backed up any more (the old `<file>.bak` copy is gone) because the managed files are unambiguously Hydra's - there is no user content at those paths to preserve. Existing machines are re-pointed by the `_SCAFFOLDED_STATUSLINE` migration in `apply_settings`, which fires **only** when `~/.claude/statusline.sh` is absent or hashes to a version Hydra shipped (`_SHIPPED_LEGACY_STATUSLINE`) - the old installer's supported customization was editing that file in place, so re-pointing a machine whose copy the user edited would honor their file while silently ceasing to run it. Dropping the block lets the template default - the new command - flow through, which is why `_SCAFFOLDED_STATUSLINE` keeps the **old** path forever. The orphaned `~/.claude/statusline.sh` left behind by the rename is deliberately **never** cleaned up - it may be the user's own file, and Hydra cannot tell. The logic is a separate `.py` for two reasons that are not style: the payload arrives on **stdin**, so a heredoc would shadow it, and `python -c` is single-quoted in the launcher, which forbids apostrophes in the source. It stays out of `hydra_cli` because it runs on every assistant message - measured on the Pi, importing the package costs 95ms against 29ms for bare python.
  - **`total_input_tokens` is the live context size, no derivation needed** - verified in the 2.1.233 binary, the payload builder returns `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` from the most recent API response. It is **0, not null**, before the first API call, so the 250k SPLIT flag must be gated on `>`, never on truthiness alone. On a 200k-window model it can never fire (auto-compact lands first); it is a 1M-context signal.
  - **The cache countdown has no field to read - it comes from the transcript.** Nothing in the status-line payload exposes a TTL, expiry, or bucket. `hydra_statusline.py` tails the last 256KB of `transcript_path`, walks back to the newest assistant record carrying `message.usage`, and takes the TTL from whichever `cache_creation` bucket is positive (1h or 5m; neither positive means the turn wrote nothing, so the TTL comes from the newest turn that DID write one - a pure cache-read writes nothing, and defaulting there reported 58m left on a 5m entry with 3m to live. The anchor still comes from the newest request, because a read refreshes the entry it hit. No write anywhere in the tail means the TTL is unknown, and unknown is rendered as no segment rather than as a guess). Measured evidence for the whole feature, from one 41-message session: a **59.7 min** idle gap still hit the cache, while **71.7** and **173.8 min** both missed with cold re-writes of 179,591 and 91,840 tokens - at Opus $5/MTok base and a 2x cache-write multiplier, ~$1.80 to rebuild what a hit would have read for ~$0.09.
  - **Anchor on the request, never the response, and never `stat`.** The cache is written when the request goes out, so the assistant record's timestamp over-reports remaining time by the whole turn duration (median 19s measured, but minutes on a subagent-heavy turn); the user record that triggered it is the closest proxy. And mtime is not a proxy at all: metadata-only records (`mode`, `ai-title`, `bridge-session`, `last-prompt`, …) are appended with **no `timestamp` field** long after the last request - one measured transcript had an mtime three days ahead of its own newest timestamped record.
  - **`refreshInterval` is load-bearing for the countdown.** The status line re-runs on session start, a new assistant message, `/compact`, a permission-mode change and a vim toggle - all of which stop happening exactly when the countdown matters. The template sets `refreshInterval: 30`.

## Conventions

- Keep it simple. No speculative abstractions - build what the next feature needs.
- No build step for the frontend. ES modules are fine if splitting JS later.
- SQL strings go in Python (no ORM). Wrap long SQL lines at 100 chars.
- Tests use `pytest` + `httpx.AsyncClient` with isolated per-test SQLite databases.
- Run `ruff check server/ tests/ client/` and `pyright server/ tests/ client/` before considering work done.
