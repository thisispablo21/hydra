"""Hydra CLI - thin client for the Hydra REST API."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from hydra_cli import api
from hydra_cli.apply_settings import cmd_apply_settings
from hydra_cli.author import author_fields
from hydra_cli.codex import (
    run_session_start as run_codex_session_start,
)
from hydra_cli.codex import (
    run_setup as run_codex_setup,
)
from hydra_cli.commands import run_pull as run_commands_pull
from hydra_cli.guard import main as run_guard_main
from hydra_cli.hooks import run_pull as run_hooks_pull
from hydra_cli.prune import cmd_project_prune
from hydra_cli.remote import cmd_capture_remote_url, scan_bridge_records
from hydra_cli.skills import HARNESSES
from hydra_cli.skills import run_pull as run_skills_pull
from hydra_cli.sync import (
    MEMORY_INDEX,
    cmd_sync,
    fetch_server_memories,
    parse_memory_file,
    resolve_project_slug,
)
from hydra_cli.usage import cmd_report as _run_usage_report
from hydra_cli.usage import run_backfill
from hydra_cli.usage_codex import cmd_reconcile as _run_usage_reconcile
from hydra_cli.usage_codex import cmd_sweep as _run_usage_sweep


def _die(status: int, body: str) -> None:
    """Print error details to stderr and exit."""
    try:
        detail = json.loads(body).get("detail", body)
    except (json.JSONDecodeError, AttributeError):
        detail = body
    print(f"Error ({status}): {detail}", file=sys.stderr)
    sys.exit(1)


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2))


def _read_body(args: argparse.Namespace) -> str:
    """Read body content from --body-file or stdin."""
    if hasattr(args, "body_file") and args.body_file:
        with open(args.body_file, encoding="utf-8") as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


# --- memory commands ---


def _brief_line(mem: dict[str, object]) -> str:
    scope = mem.get("project_slug") or "GLOBAL"
    return (
        f"{mem['id']} {mem['type']} {scope}"
        f" - {mem['name']} - {mem.get('description') or ''}"
    )


def _fetch_all_memories() -> list[dict[str, object]]:
    status, body = api.get("/api/memory")
    if status != 200:
        _die(status, body)
    return json.loads(body)


def cmd_memory_list(args: argparse.Namespace) -> None:
    """List memories - this project's plus globals, as an index, by default.

    Bodies are ~80% of the payload (the whole corpus is ~500 KB), and callers
    that only need to find a memory need the index, not the text - so brief is
    the default and --json opts back in to the full rows.
    """
    try:
        if args.all_scopes:
            memories = _fetch_all_memories()
            scope_label = "all scopes"
        elif args.globals_only:
            memories = [
                m for m in _fetch_all_memories() if m.get("project_slug") is None
            ]
            scope_label = "global"
        else:
            slug = None if args.project == "." else args.project
            if slug is None:
                # auto_attach=False: listing is read-only, and the default would
                # POST /api/projects/auto-register - creating or attaching a
                # project row as a side effect of reading.
                slug = resolve_project_slug(os.getcwd(), auto_attach=False)
                if slug is None:
                    print(
                        f"No project registered for {os.getcwd()}; showing global"
                        " memories only (use --all or --project <slug>).",
                        file=sys.stderr,
                    )
            memories = fetch_server_memories(slug)
            scope_label = f"{slug} + global" if slug else "global"
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        _print_json(memories)
        return
    for mem in memories:
        print(_brief_line(mem))
    print(f"{len(memories)} memories ({scope_label})", file=sys.stderr)


def cmd_memory_get(args: argparse.Namespace) -> None:
    status, body = api.get(f"/api/memory/{args.id}")
    if status != 200:
        _die(status, body)
    _print_json(json.loads(body))


def cmd_memory_create(args: argparse.Namespace) -> None:
    payload: dict[str, object] = {
        "name": args.name,
        "type": args.type,
    }
    if args.desc:
        payload["description"] = args.desc
    if args.project:
        payload["project_slug"] = args.project
    body_text = _read_body(args)
    if body_text:
        payload["body"] = body_text
    payload.update(author_fields(
        os.environ,
        claude_root=Path("~/.claude/projects").expanduser(),
        codex_root=Path("~/.codex/sessions").expanduser(),
        model=args.model,
    ))
    headers = {"X-Hydra-Flow": args.flow} if args.flow else None
    status, body = api.post("/api/memory", payload, headers=headers)
    if status != 200:
        _die(status, body)
    _print_json(json.loads(body))


def cmd_memory_update(args: argparse.Namespace) -> None:
    payload: dict[str, object] = {}
    if args.name:
        payload["name"] = args.name
    if args.type:
        payload["type"] = args.type
    if args.desc is not None:
        payload["description"] = args.desc
    body_text = _read_body(args)
    if body_text:
        payload["body"] = body_text
    # Re-scope in place. Without this, moving a memory between scopes meant
    # delete + re-create, which mints a new id and leaves every mirror file
    # pointing at the old one - the duplicate-memory bug.
    if args.project:
        payload["project_slug"] = args.project
    elif args.make_global:
        # A global memory needs a global type, and only the caller knows which -
        # left as type=project it would be a global row that sync re-pins to
        # whatever project the next session runs in.
        if payload.get("type") not in ("user", "feedback"):
            print(
                "--global requires --type user|feedback (a global memory cannot"
                " keep a project-scoped type)",
                file=sys.stderr,
            )
            sys.exit(1)
        payload["project_slug"] = None
    if not payload:
        print("Nothing to update", file=sys.stderr)
        sys.exit(1)
    payload.update(author_fields(
        os.environ,
        claude_root=Path("~/.claude/projects").expanduser(),
        codex_root=Path("~/.codex/sessions").expanduser(),
        model=args.model,
    ))
    headers = {"X-Hydra-Flow": args.flow} if args.flow else None
    status, body = api.put_json(
        f"/api/memory/{args.id}", payload, headers=headers
    )
    if status != 200:
        _die(status, body)
    _print_json(json.loads(body))


def cmd_memory_delete(args: argparse.Namespace) -> None:
    headers = {"X-Hydra-Flow": args.flow} if args.flow else None
    status, body = api.delete(f"/api/memory/{args.id}", headers=headers)
    if status != 204:
        _die(status, body)


# --- project commands ---


def cmd_project_list(args: argparse.Namespace) -> None:
    status, body = api.get("/api/projects")
    if status != 200:
        _die(status, body)
    _print_json(json.loads(body))


def cmd_project_get(args: argparse.Namespace) -> None:
    status, body = api.get(f"/api/projects/{args.slug}")
    if status != 200:
        _die(status, body)
    _print_json(json.loads(body))


def cmd_project_create(args: argparse.Namespace) -> None:
    payload: dict[str, str] = {
        "slug": args.slug,
        "path": args.path,
    }
    if args.desc:
        payload["description"] = args.desc
    status, body = api.post("/api/projects", payload)
    if status != 201:
        _die(status, body)
    _print_json(json.loads(body))


def cmd_project_update(args: argparse.Namespace) -> None:
    payload: dict[str, str] = {}
    if args.path:
        payload["path"] = args.path
    if args.desc is not None:
        payload["description"] = args.desc
    if not payload:
        print("Nothing to update", file=sys.stderr)
        sys.exit(1)
    status, body = api.put_json(f"/api/projects/{args.slug}", payload)
    if status != 200:
        _die(status, body)
    _print_json(json.loads(body))


def cmd_project_delete(args: argparse.Namespace) -> None:
    status, body = api.delete(f"/api/projects/{args.slug}")
    if status != 204:
        _die(status, body)


def cmd_project_attach(args: argparse.Namespace) -> None:
    """Register the current cwd to a slug. Uses cwd basename if --slug is
    omitted. Creates the project if the slug is new, otherwise adds/updates
    this machine's path row for that slug."""
    cwd = os.path.abspath(args.cwd or os.getcwd())
    slug = args.slug or os.path.basename(cwd)
    if not slug:
        print("Cannot derive slug from empty basename", file=sys.stderr)
        sys.exit(1)
    payload = {"slug": slug, "path": cwd}
    status, body = api.post("/api/projects", payload)
    if status != 201:
        _die(status, body)
    _print_json(json.loads(body))


# --- config commands ---


def cmd_config_get_claude_md(args: argparse.Namespace) -> None:
    status, body = api.get("/api/config/claude-md")
    if status != 200:
        _die(status, body)
    print(body, end="")


def cmd_config_put_claude_md(args: argparse.Namespace) -> None:
    with open(args.file, encoding="utf-8") as f:
        content = f.read()
    status, body = api.put_text("/api/config/claude-md", content)
    if status != 200:
        _die(status, body)
    _print_json(json.loads(body))


# --- commands (server-distributed slash commands) ---


def cmd_commands_pull(args: argparse.Namespace) -> None:
    sys.exit(run_commands_pull())


def cmd_commands_put(args: argparse.Namespace) -> None:
    with open(args.file, encoding="utf-8") as f:
        content = f.read()
    status, body = api.put_text(f"/api/config/commands/{args.name}", content)
    if status != 200:
        _die(status, body)
    _print_json(json.loads(body))


def cmd_commands_get(args: argparse.Namespace) -> None:
    status, body = api.get(f"/api/config/commands/{args.name}")
    if status != 200:
        _die(status, body)
    print(body, end="")


def cmd_commands_list(args: argparse.Namespace) -> None:
    status, body = api.get("/api/config/commands")
    if status != 200:
        _die(status, body)
    for name in sorted(json.loads(body)):
        print(name)


def cmd_commands_delete(args: argparse.Namespace) -> None:
    status, body = api.delete(f"/api/config/commands/{args.name}")
    if status != 204:
        _die(status, body)


# --- hooks (server-distributed policy hooks) ---


def cmd_hooks_pull(args: argparse.Namespace) -> None:
    sys.exit(run_hooks_pull(args.harness, adopt=args.adopt))


def cmd_skills_pull(args: argparse.Namespace) -> None:
    sys.exit(run_skills_pull(args.harness, adopt=args.adopt))


def cmd_codex_session_start(args: argparse.Namespace) -> None:
    sys.exit(run_codex_session_start())


def cmd_codex_setup(args: argparse.Namespace) -> None:
    sys.exit(run_codex_setup())


def cmd_guard(args: argparse.Namespace) -> None:
    run_guard_main()


def _hook_cli_error(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _hook_wiring_file(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _hook_cli_error(f"invalid hook wiring file {path}: {exc}")
    if not isinstance(data, dict):
        _hook_cli_error(f"hook wiring file must contain an object: {path}")
    extra = set(data) - {"event", "matcher", "timeout", "distribute"}
    if extra:
        _hook_cli_error(f"unknown hook wiring field {sorted(extra)[0]!r}: {path}")
    event = data.get("event")
    if not isinstance(event, str) or re.fullmatch(r"\S{1,64}", event) is None:
        _hook_cli_error(f"event must be 1..64 non-whitespace characters: {path}")
    matcher = data.get("matcher")
    if matcher is not None and (not isinstance(matcher, str) or len(matcher) > 256):
        _hook_cli_error(f"matcher must be null or at most 256 characters: {path}")
    timeout = data.get("timeout", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 600:
        _hook_cli_error(f"timeout must be an integer from 1 to 600: {path}")
    distribute = data.get("distribute", True)
    if not isinstance(distribute, bool):
        _hook_cli_error(f"distribute must be true or false: {path}")
    return {
        "event": event,
        "matcher": matcher,
        "timeout": timeout,
        "distribute": distribute,
    }


def _hook_directory_payload(args: argparse.Namespace, directory: Path) -> dict[str, object]:
    if any(
        value is not None
        for value in (args.event, args.matcher, args.runtime, args.timeout)
    ):
        _hook_cli_error("--event, --matcher, --runtime and --timeout are file-form only")
    scripts = [path for path in (directory / "hook.py", directory / "hook.sh") if path.is_file()]
    if len(scripts) != 1:
        _hook_cli_error("hook directory must contain exactly one of hook.py or hook.sh")

    wiring: dict[str, object] = {}
    for path in sorted(directory.glob("*.json")):
        if path.stem not in HARNESSES:
            _hook_cli_error(f"unknown harness wiring file: {path.name}")
        metadata = _hook_wiring_file(path)
        if metadata.pop("distribute"):
            wiring[path.stem] = metadata
    if not wiring:
        _hook_cli_error("hook directory has no distributable harness wiring")
    try:
        content = scripts[0].read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _hook_cli_error(f"cannot read hook script {scripts[0]}: {exc}")
    return {
        "content": content,
        "runtime": "python" if scripts[0].suffix == ".py" else "bash",
        "wiring": wiring,
    }


def cmd_hooks_put(args: argparse.Namespace) -> None:
    source = Path(args.file)
    if source.is_dir():
        payload = _hook_directory_payload(args, source)
    else:
        if args.event is None:
            _hook_cli_error("--event is required when publishing a hook file")
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            _hook_cli_error(f"cannot read hook script {source}: {exc}")
        payload = {
            "content": content,
            "runtime": args.runtime or "python",
            "event": args.event,
            "timeout": args.timeout if args.timeout is not None else 10,
        }
    payload["enabled"] = not args.disabled
    if args.matcher:
        payload["matcher"] = args.matcher
    if args.instances:
        payload["instances"] = [s.strip() for s in args.instances.split(",") if s.strip()]
    status, body = api.put_json(f"/api/config/hooks/{args.name}", payload)
    if status != 200:
        _die(status, body)
    _print_json(json.loads(body))


def cmd_hooks_get(args: argparse.Namespace) -> None:
    status, body = api.get(f"/api/config/hooks/{args.name}")
    if status != 200:
        _die(status, body)
    print(body, end="")


def cmd_hooks_list(args: argparse.Namespace) -> None:
    rows = []
    fetched = 0
    for harness in HARNESSES:
        status, body = api.get(f"/api/config/hooks/render/{harness}")
        if status != 200:
            print(f"hooks list [{harness}] failed ({status}): {body}", file=sys.stderr)
            continue
        fetched += 1
        payload = json.loads(body)
        for name, spec in payload.items():
            rows.append((name, harness, spec))
    if not fetched:
        sys.exit(1)
    for name, harness, spec in sorted(rows):
        flags = [] if spec.get("enabled", True) else ["disabled"]
        if spec.get("matcher"):
            flags.insert(0, f"matcher={spec['matcher']}")
        if spec.get("instances"):
            flags.append(f"instances={','.join(spec['instances'])}")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"{name}\t{harness}\t{spec.get('event', '?')}{suffix}")


def cmd_hooks_delete(args: argparse.Namespace) -> None:
    status, body = api.delete(f"/api/config/hooks/{args.name}")
    if status != 204:
        _die(status, body)


# --- doctor (instance health + stats + anomaly checks) ---


def _fmt_offenders(items: list[str], cap: int = 5) -> str:
    shown = ", ".join(items[:cap])
    extra = len(items) - cap
    return shown + (f" (+{extra} more)" if extra > 0 else "")


def _stray_memory_files(projects_root: Path) -> list[str]:
    """Return id-less or unparseable files from local memory mirrors."""
    if not projects_root.is_dir():
        return []
    strays = []
    for path in sorted(projects_root.glob("*/memory/*.md")):
        if path.name == MEMORY_INDEX or not path.is_file():
            continue
        try:
            parsed = parse_memory_file(path)
        except (OSError, UnicodeError):
            parsed = None
        if parsed is None or parsed["id"] is None:
            strays.append(str(path))
    return strays


# --- usage (token accounting) ---


def cmd_usage_report(args: argparse.Namespace) -> None:
    sys.exit(_run_usage_report(args))


def cmd_usage_backfill(args: argparse.Namespace) -> None:
    sys.exit(run_backfill(args.root))


def cmd_usage_sweep(args: argparse.Namespace) -> None:
    sys.exit(_run_usage_sweep(args))


def cmd_usage_reconcile(args: argparse.Namespace) -> None:
    sys.exit(_run_usage_reconcile(args))


def cmd_usage_summary(args: argparse.Namespace) -> None:
    query = f"?group_by={args.group_by}"
    if args.since:
        query += f"&since={args.since}"
    if args.until:
        query += f"&until={args.until}"
    status, body = api.get(f"/api/usage/summary{query}")
    if status != 200:
        _die(status, body)
    data = json.loads(body)

    def fmt(row: dict) -> str:
        tokens = sum(
            int(row[k])
            for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_5m_tokens", "cache_write_1h_tokens")
        )
        unpriced = int(row["unpriced_messages"])
        flag = f"  ({unpriced} unpriced)" if unpriced else ""
        cost = float(row["cost_usd"])
        return f"{row['key']!s:<28} {tokens:>14,} tok  ${cost:>9.2f}{flag}"

    print(f"by {data['group_by']}:")
    for row in data["rows"]:
        print(f"  {fmt(row)}")
    print(f"  {'-' * 60}")
    print(f"  {fmt(data['totals'])}")
    if data["unpriced_models"]:
        print(f"\n  unpriced models: {', '.join(data['unpriced_models'])}")


def _newest_transcript() -> Path | None:
    """Most recently modified transcript on this machine, for doctor's local check."""
    base = Path.home() / ".claude" / "projects"
    if not base.is_dir():
        return None
    files = list(base.glob("*/*.jsonl"))
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def _claude_code_version() -> str | None:
    """Last `version` stamped into the newest transcript.

    Substring scan, not a JSON parse: transcripts run to tens of MB and doctor
    only needs the value to name a version in a future drift report.
    """
    newest = _newest_transcript()
    if newest is None:
        return None
    found = None
    try:
        with open(newest, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(r'"version":"([0-9][^"]*)"', line)
                if m:
                    found = m.group(1)
    except OSError:
        return None
    return found


def cmd_doctor(
    args: argparse.Namespace, *, projects_root: Path | None = None
) -> None:
    """Deterministic instance diagnostics: connectivity, auth, stats, and data
    anomalies. Prints a compact report and exits 0 - status lives in the text,
    so a wrapper never loses the report to a non-zero exit code."""
    import urllib.error
    from collections import Counter

    url = api.base_url()
    out: list[str] = [f"Hydra doctor  -  {url}", ""]

    # 1. Connectivity (unauthenticated health probe).
    try:
        h_status, h_body = api.get("/api/health")
    except urllib.error.URLError as e:
        out += [f"server:    DOWN  ({e.reason})", "",
                "Cannot reach the server. Start it, or check HYDRA_URL."]
        print("\n".join(out))
        return
    if h_status == 200:
        db_ok = json.loads(h_body).get("db") == "ok"
        out += ["server:    UP", f"database:  {'OK' if db_ok else 'ERROR'}"]
    else:
        out.append(f"server:    DEGRADED (HTTP {h_status})")

    # 2. Auth (authenticated probe).
    token_set = bool(os.environ.get("HYDRA_AUTH_TOKEN"))
    p_status, p_body = api.get("/api/projects")
    if p_status == 401:
        out.append("auth:      FAILED (401) - HYDRA_AUTH_TOKEN unset or wrong")
        print("\n".join(out))
        return
    if p_status != 200:
        out.append(f"auth:      ERROR (HTTP {p_status} on /api/projects)")
        print("\n".join(out))
        return
    out.append(f"auth:      OK ({'token set' if token_set else 'no token / open server'})")

    proj = json.loads(p_body)
    m_status, m_body = api.get("/api/memory")
    mem = json.loads(m_body) if m_status == 200 else []

    # 3. Stats.
    pending = [p["slug"] for p in proj if p.get("auto_registered_at")]
    paths = [pt for p in proj for pt in p.get("paths", [])]
    machines = {pt.get("instance_id") for pt in paths}
    by_type = Counter(m.get("type", "?") for m in mem)
    n_global = sum(1 for m in mem if not m.get("project_slug"))
    by_proj = Counter(m["project_slug"] for m in mem if m.get("project_slug"))

    out.append("")
    out.append(
        f"projects:  {len(proj)} total ({len(pending)} pending review,"
        f" {len(proj) - len(pending)} confirmed), {len(paths)} paths /"
        f" {len(machines)} machines"
    )
    types = " | ".join(
        f"{t} {by_type.get(t, 0)}" for t in ("user", "feedback", "project", "reference")
    )
    out.append(f"memories:  {len(mem)} total ({n_global} global,"
               f" {len(mem) - n_global} pinned)")
    out.append(f"           {types}")
    if by_proj:
        out.append("top:       " + ", ".join(f"{s} {c}" for s, c in by_proj.most_common(5)))

    # 4. Remote Control URL capture.
    out.append("")
    out.append(f"remote control:  (Claude Code {_claude_code_version() or 'unknown'})")
    instance = os.environ.get("HYDRA_INSTANCE_ID", "").strip()
    s_status, s_body = api.get("/api/sessions")
    if s_status != 200:
        out.append(f"  [WARN] cannot list sessions (HTTP {s_status})")
    else:
        # SessionEnd clears the URL, so ended sessions are not a valid denominator.
        live = [
            x for x in json.loads(s_body)
            if x.get("status") != "ended"
            and (not instance or x.get("instance_id") == instance)
        ]
        got = sum(1 for x in live if x.get("remote_control_url"))
        scope = f"instance {instance}" if instance else "all instances"
        out.append(f"  server:  {got}/{len(live)} live sessions have a URL ({scope})")

    newest = _newest_transcript()
    if newest is None:
        out.append("  local:   no transcripts under ~/.claude/projects")
    else:
        scan = scan_bridge_records(str(newest))
        if not scan.records:
            out.append("  local:   newest transcript has no bridge records"
                       " (VS Code, or Remote Control never connected)")
        elif scan.url:
            out.append(f"  local:   newest transcript OK"
                       f" ({scan.records} bridge records -> URL derived)")
        elif scan.cleared:
            out.append(f"  local:   newest transcript disconnected cleanly"
                       f" ({scan.records} bridge records)")
        else:
            out.append(f"  [WARN] newest transcript has {scan.records} bridge records"
                       " but no URL derives - transcript shape drift")

    # 5. Anomaly checks (corpus invariants).
    slugs = {p["slug"] for p in proj}
    pinned_global = [
        f"#{m['id']} {m['name']} (project={m['project_slug']})"
        for m in mem
        if m.get("project_slug") and m.get("type") in ("user", "feedback")
    ]
    orphans = [
        f"#{m['id']} {m['name']} (project={m['project_slug']})"
        for m in mem
        if m.get("project_slug") and m["project_slug"] not in slugs
    ]
    pathless = [p["slug"] for p in proj if not p.get("paths")]
    if projects_root is None:
        projects_root = Path.home() / ".claude" / "projects"
    strays = _stray_memory_files(projects_root)

    def check(items: list[str], label: str) -> str:
        if not items:
            return f"  [OK]   {label}: 0"
        return f"  [WARN] {label}: {len(items)} - {_fmt_offenders(items)}"

    out.append("")
    out.append("anomalies:")
    out.append(check(pinned_global,
                     "user/feedback memories pinned to a project (type<->scope invariant)"))
    out.append(check(orphans, "memories pinned to an unregistered slug (orphans)"))
    out.append(check(pathless, "projects with no registered path"))
    out.append(check(pending, "projects pending review (auto-registered, unconfirmed)"))
    if strays:
        out.append(check(strays, "stray local memory files"))

    print("\n".join(out))


# --- argument parser ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hydra", description="Hydra CLI")
    sub = parser.add_subparsers(dest="group")

    # --- memory ---
    mem = sub.add_parser("memory")
    mem_sub = mem.add_subparsers(dest="command")

    ml = mem_sub.add_parser("list")
    ml_scope = ml.add_mutually_exclusive_group()
    ml_scope.add_argument(
        "--project", metavar="SLUG",
        help="scope to this project + globals ('.' = the project for cwd, the default)",
    )
    ml_scope.add_argument(
        "--all", dest="all_scopes", action="store_true",
        help="every scope, not just this project",
    )
    ml_scope.add_argument(
        "--global", dest="globals_only", action="store_true",
        help="global memories only",
    )
    ml.add_argument(
        "--json", action="store_true",
        help="full JSON rows including bodies (default: one index line per memory)",
    )

    mg = mem_sub.add_parser("get")
    mg.add_argument("id", type=int)

    mc = mem_sub.add_parser("create")
    mc.add_argument("--name", required=True)
    mc.add_argument("--type", required=True, choices=["user", "feedback", "project", "reference"])
    mc.add_argument("--desc", default="")
    mc.add_argument("--body-file")
    mc.add_argument("--project", help="project slug to pin this memory to (omit for global)")
    mc.add_argument("--model", help="author model override")
    mc.add_argument(
        "--flow",
        help="name of the human-gated flow this write belongs to (server requires it)",
    )

    mu = mem_sub.add_parser("update")
    mu.add_argument("id", type=int)
    mu.add_argument("--name")
    mu.add_argument("--type", choices=["user", "feedback", "project", "reference"])
    mu.add_argument("--desc")
    mu.add_argument("--body-file")
    mu.add_argument("--model", help="author model override")
    mu.add_argument(
        "--flow",
        help="name of the human-gated flow this write belongs to (server requires it)",
    )
    scope = mu.add_mutually_exclusive_group()
    scope.add_argument("--project", help="re-scope: pin this memory to a project slug")
    scope.add_argument(
        "--global", dest="make_global", action="store_true",
        help="re-scope: unpin this memory to global",
    )

    md = mem_sub.add_parser("delete")
    md.add_argument("id", type=int)
    md.add_argument(
        "--flow",
        help="name of the human-gated flow this write belongs to (server requires it)",
    )

    # --- project ---
    proj = sub.add_parser("project")
    proj_sub = proj.add_subparsers(dest="command")

    proj_sub.add_parser("list")

    pg = proj_sub.add_parser("get")
    pg.add_argument("slug")

    pc = proj_sub.add_parser("create")
    pc.add_argument("--slug", required=True)
    pc.add_argument("--path", required=True)
    pc.add_argument("--desc", default="")

    pu = proj_sub.add_parser("update")
    pu.add_argument("slug")
    pu.add_argument("--path")
    pu.add_argument("--desc")

    pd = proj_sub.add_parser("delete")
    pd.add_argument("slug")

    pa = proj_sub.add_parser(
        "attach",
        help="register cwd to a slug (idempotent; creates the slug if new)",
    )
    pa.add_argument("--slug", help="target slug (defaults to cwd basename)")
    pa.add_argument("--cwd", help="path to attach (defaults to current dir)")

    pp = proj_sub.add_parser(
        "prune", help="report registry cleanup candidates (dry-run by default)"
    )
    pp_mode = pp.add_mutually_exclusive_group()
    pp_mode.add_argument(
        "--dry-run", dest="apply", action="store_false",
        help="report only (default)",
    )
    pp_mode.add_argument(
        "--apply", action="store_true",
        help="delete eligible projects after a fresh memory check",
    )
    pp.set_defaults(apply=False)

    # --- config ---
    cfg = sub.add_parser("config")
    cfg_sub = cfg.add_subparsers(dest="command")

    cfg_sub.add_parser("get-claude-md")

    cp = cfg_sub.add_parser("put-claude-md")
    cp.add_argument("file")

    # --- commands (server-distributed slash commands) ---
    cmds = sub.add_parser("commands", help="server-distributed slash commands")
    cmds_sub = cmds.add_subparsers(dest="command")

    cmds_sub.add_parser("pull", help="hook: write server commands into ~/.claude/commands")

    cput = cmds_sub.add_parser("put", help="publish a command from a file")
    cput.add_argument("name")
    cput.add_argument("file")

    cget = cmds_sub.add_parser("get")
    cget.add_argument("name")

    cmds_sub.add_parser("list")

    cdel = cmds_sub.add_parser("delete")
    cdel.add_argument("name")

    # --- hooks (server-distributed policy hooks) ---
    hks = sub.add_parser("hooks", help="server-distributed policy hooks")
    hks_sub = hks.add_subparsers(dest="command")

    hpull = hks_sub.add_parser("pull", help="write server hooks for one harness")
    hpull.add_argument("--harness", choices=HARNESSES, default="claude-code")
    hpull.add_argument("--adopt", action="store_true")

    hput = hks_sub.add_parser("put", help="publish a hook from a file or directory")
    hput.add_argument("name")
    hput.add_argument("file")
    hput.add_argument("--event", help="e.g. PreToolUse, SubagentStart")
    hput.add_argument("--matcher", help="tool/event matcher; omit to match all")
    hput.add_argument("--runtime", choices=["python", "bash"])
    hput.add_argument("--timeout", type=int)
    hput.add_argument("--instances", help="comma-separated HYDRA_INSTANCE_IDs; omit for all")
    hput.add_argument("--disabled", action="store_true", help="store but do not distribute")

    hget = hks_sub.add_parser("get")
    hget.add_argument("name")

    hks_sub.add_parser("list")

    hdel = hks_sub.add_parser("delete")
    hdel.add_argument("name")

    # --- skills (server-distributed instructions and behavioural skills) ---
    sks = sub.add_parser("skills", help="server-distributed skills")
    sks_sub = sks.add_subparsers(dest="command")
    spull = sks_sub.add_parser("pull", help="write rendered skills for one harness")
    spull.add_argument("--harness", required=True, choices=HARNESSES)
    spull.add_argument("--adopt", action="store_true")

    usage = sub.add_parser("usage", help="token accounting")
    usage_sub = usage.add_subparsers(dest="command")

    usage_sub.add_parser(
        "report", help="hook: send this session's new token usage to Hydra"
    )
    ubf = usage_sub.add_parser(
        "backfill", help="import every transcript on this machine (re-runnable)"
    )
    ubf.add_argument("--root", help="transcript root (default ~/.claude/projects)")
    usw = usage_sub.add_parser(
        "sweep", help="scan Codex rollouts and send new token usage to Hydra"
    )
    usw.add_argument("--root", help="rollout root (default ~/.codex/sessions)")
    usw.add_argument("--reset", action="store_true", help="re-send every rollout")
    ureconcile = usage_sub.add_parser(
        "reconcile", help="reparse and reconcile historical usage"
    )
    ureconcile_sub = ureconcile.add_subparsers(dest="reconcile_harness", required=True)
    urec_codex = ureconcile_sub.add_parser("codex", help="reconcile Codex rollouts")
    urec_codex.add_argument("--root", help="rollout root (default ~/.codex/sessions)")
    urec_codex.add_argument("--apply", action="store_true", help="apply the previewed changes")
    usm = usage_sub.add_parser("summary", help="print aggregated usage")
    usm.add_argument(
        "--group-by",
        dest="group_by",
        default="day",
        choices=["day", "model", "project", "instance", "harness", "agent"],
    )
    usm.add_argument("--since", help="ISO date/datetime, inclusive")
    usm.add_argument("--until", help="ISO date/datetime, exclusive")

    # --- sync ---
    sync = sub.add_parser("sync", help="pull memories into the local mirror")
    sync.add_argument("--pull", action="store_true", help="compatibility no-op")
    sync.add_argument("--cwd", help="override cwd (hooks pass $PWD)")
    sync.add_argument("--dry-run", action="store_true")

    sub.add_parser("codex-session-start", help="Codex SessionStart hook entry")
    sub.add_parser("codex-setup", help="wire the Codex SessionStart hook")
    sub.add_parser("guard", help="deny memory writes outside a human-gated flow")

    # --- doctor (instance health + stats + anomaly checks) ---
    sub.add_parser("doctor", help="diagnose this Hydra instance (health, stats, anomalies)")

    # --- capture-remote-url (Stop hook entry; reads payload from stdin) ---
    sub.add_parser(
        "capture-remote-url",
        help="hook: scan transcript for /remote-control URL and PUT it to Hydra",
    )

    # --- apply-settings (setup.sh entry; merges Hydra template + user prefs) ---
    aps = sub.add_parser(
        "apply-settings",
        help="merge Hydra hooks template + user prefs into ~/.claude/settings.json",
    )
    aps.add_argument("--hydra-template", required=True)
    aps.add_argument("--user-template", required=True)
    aps.add_argument("--user-file", required=True)
    aps.add_argument(
        "--hooks-layer",
        help="generated server-hooks layer (settings.hooks.json); missing = none",
    )
    aps.add_argument("--output", required=True)
    aps.add_argument("--hydra-url", required=True)
    aps.add_argument("--hydra-repo-path", required=True)

    return parser


DISPATCH = {
    ("memory", "list"): cmd_memory_list,
    ("memory", "get"): cmd_memory_get,
    ("memory", "create"): cmd_memory_create,
    ("memory", "update"): cmd_memory_update,
    ("memory", "delete"): cmd_memory_delete,
    ("project", "list"): cmd_project_list,
    ("project", "get"): cmd_project_get,
    ("project", "create"): cmd_project_create,
    ("project", "update"): cmd_project_update,
    ("project", "delete"): cmd_project_delete,
    ("project", "attach"): cmd_project_attach,
    ("project", "prune"): cmd_project_prune,
    ("config", "get-claude-md"): cmd_config_get_claude_md,
    ("config", "put-claude-md"): cmd_config_put_claude_md,
    ("commands", "pull"): cmd_commands_pull,
    ("commands", "put"): cmd_commands_put,
    ("commands", "get"): cmd_commands_get,
    ("commands", "list"): cmd_commands_list,
    ("commands", "delete"): cmd_commands_delete,
    ("hooks", "pull"): cmd_hooks_pull,
    ("hooks", "put"): cmd_hooks_put,
    ("hooks", "get"): cmd_hooks_get,
    ("hooks", "list"): cmd_hooks_list,
    ("hooks", "delete"): cmd_hooks_delete,
    ("skills", "pull"): cmd_skills_pull,
    ("usage", "report"): cmd_usage_report,
    ("usage", "backfill"): cmd_usage_backfill,
    ("usage", "sweep"): cmd_usage_sweep,
    ("usage", "reconcile"): cmd_usage_reconcile,
    ("usage", "summary"): cmd_usage_summary,
    ("sync", None): cmd_sync,
    ("codex-session-start", None): cmd_codex_session_start,
    ("codex-setup", None): cmd_codex_setup,
    ("guard", None): cmd_guard,
    ("doctor", None): cmd_doctor,
    ("capture-remote-url", None): cmd_capture_remote_url,
    ("apply-settings", None): cmd_apply_settings,
}


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    parser = build_parser()
    args = parser.parse_args()

    if not args.group:
        parser.print_help()
        sys.exit(1)

    # `sync`, `capture-remote-url`, and `apply-settings` are leaf commands;
    # others need a subcommand.
    command = getattr(args, "command", None)
    leaf_groups = {
        "sync",
        "doctor",
        "capture-remote-url",
        "apply-settings",
        "codex-session-start",
        "codex-setup",
        "guard",
    }
    if args.group not in leaf_groups and not command:
        parser.print_help()
        sys.exit(1)

    handler = DISPATCH.get((args.group, command))
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
