"""Pull Hydra server memories into Claude Code filesystem memory dirs.

Scope rule (locked in plan): memories with type user/feedback are global
(project_slug=NULL); memories with type project/reference are pinned to the
project derived from the session cwd.

Identity: a memory is its server row id, which pull stamps into the mirror
file's frontmatter as `id:`. The mirror is read-only provenance; memories are
edited on the server through the dashboard or CLI.

`hydra sync` and `hydra sync --pull` download all server memories, overwrite
canonical mirror files, and prune files the server has superseded. Pruning is
skipped for unregistered cwds, which have no authoritative server view. Pull
never prunes an unparseable file or an id-less file whose name is absent from
the server.

Nothing destructive runs against an empty server view: a wrong HYDRA_URL, a
fresh DB and a restored backup all look exactly like "everything was deleted".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from hydra_cli import api
from hydra_cli import memory_routing as routing
from hydra_cli.paths import is_contained_by, path_shape

GLOBAL_TYPES = {"user", "feedback"}
PROJECT_TYPES = {"project", "reference"}
VALID_TYPES = GLOBAL_TYPES | PROJECT_TYPES
MEMORY_INDEX = "MEMORY.md"

EMITTED_FM_KEYS = ("id", "name", "description", "type", "updated_at")
# Frontmatter keys that record where a mirror file came from, rather than what
# the memory says. Blank ones are never emitted.
PROVENANCE_FM_KEYS = ("id", "updated_at")


# --- Filesystem helpers ---


def memory_dir_for_cwd(cwd: str) -> Path:
    """Map a project cwd to Claude Code's local memory dir.

    Claude Code encodes the project path by replacing EVERY non-alphanumeric
    character with `-` (not just the path separators). E.g.
    /home/giosue/projects/hydra → -home-giosue-projects-hydra,
    C:\\Users\\giosu\\projects\\pcb → C--Users-giosu-projects-pcb,
    /home/me/foo_bar → -home-me-foo-bar, /home/me/my.proj → -home-me-my-proj.

    This mirrors Claude Code's own encoder (`x.replace(/[^a-zA-Z0-9]/g, "-")`);
    a slug that keeps `_`/`.` would point at a nonexistent dir and sync 0 files
    silently. CC also truncates + hashes paths over 200 chars; that behavior is
    not replicated here.
    """
    cwd = os.path.abspath(cwd)
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    return Path.home() / ".claude" / "projects" / slug / "memory"


def _base_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower() or "memory"


def canonical_filenames(memories: list[dict[str, Any]]) -> dict[int, str]:
    """Map each server row id to its mirror filename, over the WHOLE set at once.

    _base_slug is not injective - it lowercases and collapses every run of
    non-alphanumerics to '_', so 'Keep Hydra deployment-agnostic' and
    'keep-hydra-deployment-agnostic' both want keep_hydra_deployment_agnostic.md.
    Computed per row, two memories silently clobber one file and the loser
    becomes a zombie: present in every API listing, absent from the mirror,
    unfixable by editing files. So the first claimant (lowest id) keeps
    <slug>.md and later ones get <slug>-<id>.md - injective by construction
    (base slugs never contain '-'), and identical on every machine.
    """
    out: dict[int, str] = {}
    taken: set[str] = set()
    for mem in sorted(memories, key=lambda m: m["id"]):
        slug = _base_slug(mem["name"])
        filename = f"{slug}.md"
        if filename in taken:
            filename = f"{slug}-{mem['id']}.md"
        taken.add(filename)
        out[mem["id"]] = filename
    return out


def stamp_provenance(path: Path, mem: dict[str, Any]) -> None:
    """Write the server's `id` and `updated_at` into a file's frontmatter, in place.

    Deliberately a surgical line edit, not a re-serialize: the file may carry
    frontmatter keys we don't emit (Claude Code's memory tool writes
    originSessionId) and body bytes we have no business round-tripping.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return  # unterminated frontmatter; leave it alone
    for key in PROVENANCE_FM_KEYS:
        value = mem.get(key)
        if not value:
            continue
        line = f"{key}: {value}\n"
        for i in range(1, end):
            if lines[i].split(":", 1)[0].strip() == key:
                lines[i] = line
                break
        else:
            lines.insert(1, line)
            end += 1
    path.write_text("".join(lines), encoding="utf-8")


# --- Frontmatter ---


def parse_memory_file(path: Path) -> dict[str, Any] | None:
    """Parse a memory .md file; return dict with id/name/description/type/body or
    None if the frontmatter is missing or malformed.

    A missing or non-integer `id` is not a parse failure. It yields id=None so
    pull can preserve a local-only file that has no server counterpart.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None

    fm: dict[str, str] = {}
    for raw in lines[1:end]:
        if ":" not in raw:
            continue
        k, _, v = raw.partition(":")
        value = v.strip()
        if k.strip() not in {"topics", "project_slug"} and value.startswith('"'):
            try:
                decoded = json.loads(value)
                if isinstance(decoded, str):
                    value = decoded
            except json.JSONDecodeError:
                pass
        fm[k.strip()] = value

    body = "\n".join(lines[end + 1:]).lstrip("\n")
    if "name" not in fm or "type" not in fm:
        return None
    if fm["type"] not in VALID_TYPES:
        return None
    try:
        mem_id = int(fm["id"])
    except (KeyError, ValueError):
        mem_id = None
    routing_invalid = False
    try:
        topics = json.loads(fm.get("topics", "null"))
        if topics is not None and (
            not isinstance(topics, list) or any(not isinstance(t, str) for t in topics)
        ):
            routing_invalid = True
            topics = None
    except json.JSONDecodeError:
        topics = None
        routing_invalid = True
    try:
        project_slug = json.loads(fm.get("project_slug", "null"))
        if project_slug is not None and not isinstance(project_slug, str):
            raise ValueError("Invalid project scope")
    except (json.JSONDecodeError, ValueError):
        project_slug = None
        routing_invalid = True
    return {
        "topics": topics,
        "project_slug": project_slug,
        "routing_scope_known": "project_slug" in fm,
        "routing_invalid": routing_invalid,
        "id": mem_id,
        "name": fm["name"],
        "description": fm.get("description", ""),
        "type": fm["type"],
        "body": body,
        # Provenance only: the row version this mirror file was written from.
        "updated_at": fm.get("updated_at", ""),
    }


def serialize_memory(mem: dict[str, Any]) -> str:
    """Render a memory as frontmatter + body text (inverse of parse_memory_file)."""
    lines = ["---"]
    for k in EMITTED_FM_KEYS:
        if k in PROVENANCE_FM_KEYS and not mem.get(k):
            continue  # a blank id/updated_at parses back as unknown; don't emit one
        value = mem.get(k, '')
        if isinstance(value, str) and any(char in value for char in ('\n', '\r', '"')):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{k}: {value}")
    lines.append("topics: " + json.dumps(mem.get("topics"), ensure_ascii=False))
    lines.append("project_slug: " + json.dumps(mem.get("project_slug"), ensure_ascii=False))
    lines.append("---")
    body = mem.get("body") or ""
    if not body.endswith("\n"):
        body += "\n"
    return "\n".join(lines) + "\n" + body


# --- Scope derivation ---


def scope_is_global(mem_type: str) -> bool:
    return mem_type in GLOBAL_TYPES


def effective_project_slug(mem_type: str, current_slug: str | None) -> str | None:
    """Global types ignore current_slug; project types use it."""
    return None if scope_is_global(mem_type) else current_slug


# --- Server I/O ---


def _api_error(status: int, body: str) -> str:
    try:
        return json.loads(body).get("detail", body)
    except (json.JSONDecodeError, AttributeError):
        return body


def resolve_project_slug(cwd: str, *, auto_attach: bool = True) -> str | None:
    """Look up the project slug for cwd in the Hydra projects registry.

    Matches against any registered path for any machine - a project may live
    at different filesystem paths on different machines, and the cwd itself
    is unambiguous enough that instance_id scoping is unnecessary here.

    A read-only lookup falls back to the deepest confirmed ancestor. If
    `auto_attach` is set instead, the server owns containment and registration
    through `/api/projects/auto-register`.
    """
    status, body = api.get("/api/projects")
    if status != 200:
        raise RuntimeError(f"Failed to fetch projects: {_api_error(status, body)}")
    projects = json.loads(body)
    target = os.path.abspath(cwd)
    for p in projects:
        for entry in p.get("paths", []):
            if os.path.abspath(entry["path"]) == target:
                return p["slug"]

    if not auto_attach:
        matches = [
            (p["slug"], entry["path"])
            for p in projects
            if p.get("auto_registered_at") is None
            for entry in p.get("paths", [])
            if is_contained_by(target, entry["path"])
        ]
        if not matches:
            return None
        deepest = max(len(path_shape(path)[1]) for _, path in matches)
        slugs = {
            slug
            for slug, path in matches
            if len(path_shape(path)[1]) == deepest
        }
        return slugs.pop() if len(slugs) == 1 else None

    return _auto_register(target)


def _auto_register(cwd: str) -> str | None:
    """POST to /api/projects/auto-register. Returns the slug on
    created/attached/existing; None on skipped (with the server's reason
    printed to stderr)."""
    status, body = api.post("/api/projects/auto-register", {"cwd": cwd})
    if status != 200:
        raise RuntimeError(
            f"Failed to auto-register {cwd}: {_api_error(status, body)}"
        )
    resp = json.loads(body)
    if resp["status"] == "skipped":
        print(
            f"  auto-register skipped for {cwd}: {resp.get('reason') or 'unspecified'}",
            file=sys.stderr,
        )
        return None
    if resp["status"] in ("created", "attached"):
        print(
            f"  auto-{resp['status']} {cwd} as project '{resp['slug']}'"
            " (review on dashboard)",
            file=sys.stderr,
        )
    return resp.get("slug")


def fetch_server_memories(project_slug: str | None) -> list[dict[str, Any]]:
    """Fetch memories relevant to this project: pinned + globals. If no
    project_slug, only globals."""
    if project_slug is None:
        status, body = api.get("/api/memory")
        if status != 200:
            raise RuntimeError(f"Failed to list memories: {_api_error(status, body)}")
        return [m for m in json.loads(body) if m.get("project_slug") is None]

    path = f"/api/memory?project_slug={quote(project_slug)}&include_global=true"
    status, body = api.get(path)
    if status != 200:
        raise RuntimeError(f"Failed to list memories: {_api_error(status, body)}")
    return json.loads(body)


def fetch_whole_corpus() -> list[dict[str, Any]] | None:
    """Every memory on the server, across every scope. None if unreadable.

    A project with no memories of its own is normal and prunes fine. A server
    with no memories at all may be a wrong HYDRA_URL, a fresh DB or a restored
    backup, and must never authorize deletion.
    """
    status, body = api.get("/api/memory")
    if status != 200:
        return None
    return json.loads(body)


# --- MEMORY.md regeneration ---


def regenerate_index(
    memory_dir: Path, entries: list[tuple[Path, dict[str, Any]]]
) -> None:
    """Write MEMORY.md as a flat bullet list sorted by name.

    Links to the filename each memory was actually found at. The caller decides
    whether the server view is authoritative enough to filter disk entries.
    """
    lines = []
    for path, mem in sorted(entries, key=lambda e: e[1]["name"]):
        desc = mem.get("description", "").strip()
        suffix = f" - {desc}" if desc else ""
        lines.append(f"- [{mem['name']}]({path.name}){suffix}")
    content = "\n".join(lines) + ("\n" if lines else "")
    routing.warn_root_budget(content)
    routing.atomic_write(memory_dir / MEMORY_INDEX, content)


# --- Filesystem walk ---


def walk_local_memories(memory_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Return [(path, parsed_dict)] for every valid memory file. Skips
    MEMORY.md and unparseable files."""
    if not memory_dir.is_dir():
        return []
    out = []
    for p in sorted(memory_dir.iterdir()):
        if p.is_symlink() and p.suffix == ".md":
            raise ValueError(f"Refusing mirror symlink: {p}")
        if p.name == MEMORY_INDEX or p.suffix != ".md" or not p.is_file():
            continue
        parsed = parse_memory_file(p)
        if parsed is not None:
            out.append((p, parsed))
    return out


# --- Orchestrator ---


def _run_legacy_sync(cwd: str, *, dry_run: bool = False) -> int:
    """Pull server memories into cwd's mirror. Return 0 on success."""
    current_slug = resolve_project_slug(cwd, auto_attach=not dry_run)
    memory_dir = memory_dir_for_cwd(cwd)

    server = fetch_server_memories(current_slug)
    routing.validate_snapshot({
        "format_version": 1, "topics": [], "corpus_nonempty": bool(server),
        "memories": [{**mem, "topics": None} for mem in server],
    }, current_slug)
    server_by_name = {m["name"]: m for m in server}
    filenames = canonical_filenames(server)
    canonical = set(filenames.values())

    # The whole corpus, fetched at most once and only when the scoped view can't
    # answer the question. An empty server is never authority to delete: a wrong
    # HYDRA_URL, a fresh DB and a restored backup are indistinguishable from
    # "everything was deleted", and the mirror may be the only surviving copy.
    # A project that merely has no memories OF ITS OWN is a different thing and
    # prunes fine, hence corpus-wide rather than scope-wide.
    corpus_cache: dict[str, list[dict[str, Any]] | None] = {}

    def corpus() -> list[dict[str, Any]] | None:
        if "rows" not in corpus_cache:
            corpus_cache["rows"] = fetch_whole_corpus()
        return corpus_cache["rows"]

    authoritative = bool(server) or bool(corpus())
    pulled = pruned = 0
    routing.check_paths(memory_dir, list(canonical), [])
    for filename in canonical:
        target = memory_dir / filename
        if target.exists() and parse_memory_file(target) is None:
            raise ValueError(f"Refusing unmanaged body collision: {target}")
    if not dry_run:
        memory_dir.mkdir(parents=True, exist_ok=True)
    for remote in server:
        target = memory_dir / filenames[remote["id"]]
        if dry_run:
            print(f"  would pull: {remote['name']} -> {target.name}")
        else:
            routing.atomic_write(target, serialize_memory(remote))
            print(f"  pulled: {remote['name']}")
        pulled += 1

    # A registered cwd and a non-empty corpus make the scoped server view safe
    # to prune against. Empty servers never authorize deletion.
    obsolete: list[Path] = []
    if current_slug is not None and authoritative:
        for path in sorted(memory_dir.glob("*.md")):
            if path.name == MEMORY_INDEX or path.name in canonical:
                continue
            parsed = parse_memory_file(path)
            if parsed is None:
                print(
                    f"  keep (not a memory file, leaving alone): {path.name}",
                    file=sys.stderr,
                )
                continue
            if parsed["id"] is None and parsed["name"] not in server_by_name:
                print(
                    f"  keep (local-only, not on the server - rename it or"
                    f" resolve the name clash): {path.name}",
                    file=sys.stderr,
                )
                continue
            if dry_run:
                print(f"  would prune (superseded by the server): {path.name}")
            else:
                obsolete.append(path)
            pruned += 1

    if dry_run:
        entries = [(memory_dir / filenames[mem["id"]], mem) for mem in server]
        if not authoritative:
            entries = walk_local_memories(memory_dir)
        lines = []
        for path, mem in sorted(entries, key=lambda entry: entry[1]["name"]):
            desc = mem.get("description", "").strip()
            suffix = f" - {desc}" if desc else ""
            lines.append(f"- [{mem['name']}]({path.name}){suffix}")
        routing.preview("\n".join(lines) + ("\n" if lines else ""), {}, ["Legacy flat index"])
    else:
        entries = walk_local_memories(memory_dir)
        if authoritative:
            entries = [(path, mem) for path, mem in entries if path.name in canonical]
        regenerate_index(memory_dir, entries)
        for path in obsolete:
            path.unlink()
            print(f"  pruned (superseded by the server): {path.name}")

    print(f"\nSummary: {pulled} pulled, {pruned} pruned")
    return 0


# --- CLI entry point ---


def _run_snapshot_sync(
    current_slug: str | None, memory_dir: Path, snapshot: dict[str, Any], *, dry_run: bool,
) -> int:
    server = snapshot["memories"]
    topics = snapshot["topics"]
    authoritative = snapshot["corpus_nonempty"]
    filenames = canonical_filenames(server)
    canonical = set(filenames.values())
    server_names = {mem["name"] for mem in server}
    local = walk_local_memories(memory_dir)
    entries = [(memory_dir / filenames[mem["id"]], mem) for mem in server]
    if not authoritative:
        entries = local
        parsed_names = {path.name for path, _ in local}
        for path in sorted(memory_dir.glob("*.md")):
            if path.name != MEMORY_INDEX and path.name not in parsed_names and path.is_file():
                entries.append((path, {
                    "name": path.stem, "description": "Unparseable local file - preserved",
                    "topics": None, "project_slug": None, "routing_invalid": True,
                }))
        prior = routing.load_manifest(memory_dir)
        if prior:
            topics = prior["topics"]
    root, indexes, diagnostics = routing.render_indexes(
        entries, topics, current_slug, recovery=not authoritative,
    )
    routing.check_paths(memory_dir, list(canonical), list(indexes))
    for name in canonical:
        target = memory_dir / name
        if target.exists() and parse_memory_file(target) is None:
            raise ValueError(f"Refusing unmanaged body collision: {target}")
    obsolete = []
    if current_slug is not None and authoritative:
        obsolete = [path for path, mem in local if path.name not in canonical
                    and (mem["id"] is not None or mem["name"] in server_names)]
    previous = routing.load_manifest(memory_dir)
    if previous:
        old_active = set(previous.get("active_files", previous["files"]))
        retired = old_active - indexes.keys()
        links = previous.get("index_bodies", {})
        retained = {body for name in retired for body in links.get(name, [])}
        obsolete = [path for path in obsolete if path.name not in retained]
    if dry_run:
        routing.preview(root, indexes, diagnostics)
        for path in obsolete:
            print(f"  would prune (superseded by the server): {path.name}")
        print(f"\nSummary: {len(server)} would pull, {len(obsolete)} would prune")
        return 0
    memory_dir.mkdir(parents=True, exist_ok=True)
    for mem in server:
        routing.atomic_write(memory_dir / filenames[mem["id"]], serialize_memory(mem))
    retained_bodies = routing.publish_indexes(memory_dir, root, indexes, topics)
    # Old roots and indexes stay usable until publication succeeds.
    for path in obsolete:
        if path.name in retained_bodies:
            continue
        if path.is_symlink():
            raise ValueError(f"Refusing obsolete body symlink: {path}")
        path.unlink()
    for diagnostic in diagnostics:
        print(f"  {diagnostic}", file=sys.stderr)
    print(f"\nSummary: {len(server)} pulled, {len(obsolete)} pruned")
    return 0


def run_sync(cwd: str, *, dry_run: bool = False) -> int:
    """Validate a coherent scoped snapshot before publishing any local files."""
    memory_dir = memory_dir_for_cwd(cwd)
    current_slug = resolve_project_slug(cwd, auto_attach=not dry_run)
    query = f"?project_slug={quote(current_slug)}" if current_slug else ""
    status, body = api.get("/api/memory/snapshot" + query)
    if status in (404, 422):
        if routing.load_manifest(memory_dir) is not None:
            raise RuntimeError("Server lacks routing snapshots; keeping last-good memory router")
        print("Server lacks routing snapshots; using legacy flat index", file=sys.stderr)
        return _run_legacy_sync(cwd, dry_run=dry_run)
    if status != 200:
        raise RuntimeError(f"Failed to fetch memory snapshot: {_api_error(status, body)}")
    snapshot = routing.validate_snapshot(json.loads(body), current_slug)
    return _run_snapshot_sync(current_slug, memory_dir, snapshot, dry_run=dry_run)


def cmd_sync(args: argparse.Namespace) -> None:
    # CLAUDE_PROJECT_DIR is the session's launch dir and stays put; $PWD/getcwd
    # follow the model's `cd` and would auto-register subdirs as projects.
    cwd = args.cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    exit_code = run_sync(cwd, dry_run=args.dry_run)
    sys.exit(exit_code)
