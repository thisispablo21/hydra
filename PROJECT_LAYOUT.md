# Hydra project layout (moved from CLAUDE.md)

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
                        POST /api/usage/reconcile/codex (preview or owned-row repair),
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
    usage_codex.py         - Codex rollout parser + sweep/reconcile, shared atomic offset
                              state, root-session batching and server capability handshake
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
  test_usage_codex.py - Codex cumulative parser, guardian alias kept unpriced, sweep state,
                        reconciliation, batching and redacted rollout corpus canary
  test_usage_report.py - `usage report`: record→message dedupe, subagent discovery,
                        symlinked workflow dirs, offsets (failure, partial line, truncation)
schema.sql            - DDL; sessions.archived_at, memories project_slug FK + UNIQUE(name)
                        (inline; db._migrate installs it on legacy DBs - see Key Patterns),
                        skills + skill_variants,
                        config_commands (server-distributed slash commands),
                        config_hooks (server-distributed policy hooks: script + wiring),
                        usage_messages (per-API-message token counts, PK message_id, service_tier)
```
