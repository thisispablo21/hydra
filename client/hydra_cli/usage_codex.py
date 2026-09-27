"""Report Codex rollout token usage to Hydra."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from hydra_cli import api
from hydra_cli.usage import CHUNK, state_dir

_STATE_NAME = "codex-sweep.json"
_LONG_CONTEXT = 272_000
_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


@dataclass
class ParseResult:
    rows: list[dict[str, Any]]
    offset: int
    session_id: str | None
    usage_events: int = 0
    skipped_without_turn: int = 0
    long_context_calls: int = 0
    ambiguous_first_usage: int = 0


def _state_path() -> Path:
    return state_dir() / _STATE_NAME


def _load_offsets() -> dict[str, int]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: value
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, int) and value >= 0
    }


def _save_offsets(offsets: dict[str, int]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(offsets, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _record(raw: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {key: 0 for key in _USAGE_KEYS}
    return {key: int(value.get(key) or 0) for key in _USAGE_KEYS}


def _agent_type(source: Any) -> str | None:
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent")
    if subagent == "review":
        return "review"
    if not isinstance(subagent, dict):
        return None
    if subagent.get("other") == "guardian":
        return "guardian"
    if isinstance(subagent.get("thread_spawn"), dict):
        return "spawn"
    return None


def parse_file(
    path: str,
    offset: int = 0,
) -> ParseResult:
    """Rebuild rollout state from byte zero and emit complete records after offset."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ParseResult([], offset, None)
    emit_from = 0 if size < offset else offset

    rows: list[dict[str, Any]] = []
    pos = 0
    session_id = None
    thread_id = None
    parent_thread_id = None
    source: Any = None
    meta_cwd = None
    model = None
    effort = None
    cwd = None
    previous: dict[str, int] | None = None
    usage_events = 0
    skipped_without_turn = 0
    long_context_calls = 0
    ambiguous_first_usage = 0
    service_tier = None

    try:
        with open(path, "rb") as handle:
            for raw in handle:
                if not raw.endswith(b"\n"):
                    break
                record_start = pos
                pos += len(raw)
                rec = _record(raw)
                if not rec:
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue

                if rec.get("type") == "session_meta":
                    if session_id is None:
                        session_id = payload.get("session_id")
                        thread_id = payload.get("id")
                        parent_thread_id = payload.get("parent_thread_id")
                        source = payload.get("source")
                        meta_cwd = payload.get("cwd")
                    continue

                if rec.get("type") == "turn_context":
                    # Guardian reviews report "codex-auto-review", a hidden routing alias
                    # with no public backing model: kept as-is so it stays unpriced.
                    model = payload.get("model")
                    effort = payload.get("effort")
                    cwd = payload.get("cwd")
                    continue

                if payload.get("type") == "thread_settings_applied":
                    settings = payload.get("thread_settings")
                    if isinstance(settings, dict):
                        tier = settings.get("service_tier")
                        if isinstance(tier, str) and tier:
                            service_tier = tier
                    continue

                if rec.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                usage_events += 1
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                cumulative = _usage(info.get("total_token_usage"))
                raw_last = info.get("last_token_usage")
                last = _usage(raw_last)
                if previous is None:
                    previous = cumulative
                    if not isinstance(raw_last, dict):
                        if record_start >= emit_from:
                            ambiguous_first_usage += 1
                        continue
                    delta = last
                elif cumulative != previous and (
                    cumulative == last
                    or any(cumulative[key] < previous[key] for key in _USAGE_KEYS)
                ):
                    # A resumed counter starts at the latest call, even if it
                    # exceeds the old total. Unchanged snapshots remain duplicates.
                    delta = last
                else:
                    delta = {
                        key: cumulative[key] - previous[key] for key in _USAGE_KEYS
                    }
                previous = cumulative

                if record_start < emit_from or delta["total_tokens"] == 0:
                    continue
                if not isinstance(model, str) or not model:
                    skipped_without_turn += 1
                    continue
                timestamp = rec.get("timestamp")
                if not all(isinstance(v, str) and v for v in (session_id, thread_id, timestamp)):
                    continue

                window = int(info.get("model_context_window") or 0)
                if delta["input_tokens"] > _LONG_CONTEXT or window > _LONG_CONTEXT:
                    long_context_calls += 1
                rows.append(
                    {
                        "message_id": (
                            f"codex:{thread_id}:{timestamp}:{cumulative['total_tokens']}"
                        ),
                        "ts": timestamp,
                        "model": model,
                        "harness": "codex-cli",
                        "cwd": cwd if isinstance(cwd, str) else meta_cwd,
                        "effort": effort,
                        "is_subagent": parent_thread_id is not None,
                        "agent_type": _agent_type(source),
                        "service_tier": None,
                        "speed": None,
                        "input_tokens": (
                            delta["input_tokens"] - delta["cached_input_tokens"]
                        ),
                        "output_tokens": delta["output_tokens"],
                        "cache_read_tokens": delta["cached_input_tokens"],
                        "cache_write_5m_tokens": delta["cache_write_input_tokens"],
                        "cache_write_1h_tokens": 0,
                        "web_search_requests": 0,
                        "web_fetch_requests": 0,
                    }
                )
    except OSError as exc:
        print(f"hydra usage sweep: cannot read {path}: {exc}", file=sys.stderr)
        return ParseResult([], offset, None)

    # `thread_settings_applied` is emitted once the thread applies settings, so
    # usage rows can precede it (751 of them in the measured corpus). The tier is
    # constant per rollout - no file in 259 showed two - so the value observed
    # anywhere in the file is the value for every row in it. Absent stays None,
    # which prices as default.
    for row in rows:
        row["service_tier"] = service_tier

    return ParseResult(
        rows,
        pos,
        session_id if isinstance(session_id, str) else None,
        usage_events,
        skipped_without_turn,
        long_context_calls,
        ambiguous_first_usage,
    )


def _handshake() -> bool:
    query = urlencode(
        {"group_by": "harness", "since": datetime.now(UTC).isoformat()}
    )
    status, _body = api.get(f"/api/usage/summary?{query}")
    if status == 200:
        return True
    print(
        f"hydra usage sweep: server does not support harness usage ({status}); no rows sent",
        file=sys.stderr,
    )
    return False


def run_sweep(root: str | None = None, *, reset: bool = False) -> int:
    base = Path(root) if root else Path.home() / ".codex" / "sessions"
    if not base.is_dir():
        print(f"hydra usage sweep: no rollout root at {base}", file=sys.stderr)
        return 1

    paths = sorted(base.rglob("rollout-*.jsonl"))
    offsets = {} if reset else _load_offsets()
    pending: dict[str, int] = {}
    batches: dict[str, list[dict[str, Any]]] = {}
    scanned = 0
    no_usage = 0
    skipped_without_turn = 0
    long_context_calls = 0
    ambiguous_first_usage = 0

    for path in paths:
        path_str = str(path)
        old_offset = offsets.get(path_str, 0)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == old_offset:
            pending[path_str] = old_offset
            continue

        scanned += 1
        result = parse_file(path_str, old_offset)
        pending[path_str] = result.offset
        no_usage += result.usage_events == 0
        skipped_without_turn += result.skipped_without_turn
        long_context_calls += result.long_context_calls
        ambiguous_first_usage += result.ambiguous_first_usage
        if result.session_id and result.rows:
            batches.setdefault(result.session_id, []).extend(result.rows)

    rows = sum(len(messages) for messages in batches.values())
    if rows and not _handshake():
        return 0

    for session_id, messages in batches.items():
        for start in range(0, len(messages), CHUNK):
            status, body = api.post(
                "/api/usage/messages",
                {"session_id": session_id, "messages": messages[start : start + CHUNK]},
            )
            if status not in (200, 204):
                print(
                    f"hydra usage sweep: POST failed ({status}): {body}",
                    file=sys.stderr,
                )
                return 1

    try:
        _save_offsets(pending)
    except OSError as exc:
        print(f"hydra usage sweep: cannot save state: {exc}", file=sys.stderr)
        return 1
    print(
        f"hydra usage sweep: {rows} rows from {scanned} changed files;"
        f" {no_usage} files with no usage events;"
        f" {skipped_without_turn} events before turn context;"
        f" {long_context_calls} long-context calls;"
        f" {ambiguous_first_usage} ambiguous first usage events",
        file=sys.stderr,
    )
    return 0


_RECONCILE_TOTALS = (
    "inserted",
    "updated",
    "unchanged",
    "foreign_unchanged",
    "conflicts",
)


def _empty_reconcile_totals() -> dict[str, int]:
    return {key: 0 for key in _RECONCILE_TOTALS}


def _print_reconcile_totals(totals: dict[str, int], phase: str) -> None:
    print(
        "hydra usage reconcile codex:"
        f" inserted {totals['inserted']};"
        f" updated {totals['updated']};"
        f" unchanged {totals['unchanged']};"
        f" foreign-unchanged {totals['foreign_unchanged']};"
        f" conflicts {totals['conflicts']} ({phase})"
    )


def _reconcile_request(
    messages: list[dict[str, Any]], *, apply: bool
) -> dict[str, int] | None:
    try:
        status, body = api.post(
            "/api/usage/reconcile/codex",
            {"apply": apply, "messages": messages},
        )
    except OSError as exc:
        print(
            f"hydra usage reconcile codex: POST failed: {exc}",
            file=sys.stderr,
        )
        return None
    if status != 200:
        print(
            f"hydra usage reconcile codex: POST failed ({status}): {body}",
            file=sys.stderr,
        )
        return None
    try:
        response = json.loads(body)
        totals = {key: response[key] for key in _RECONCILE_TOTALS}
    except (json.JSONDecodeError, KeyError, TypeError):
        print(
            "hydra usage reconcile codex: malformed server response",
            file=sys.stderr,
        )
        return None
    if any(not isinstance(value, int) or value < 0 for value in totals.values()):
        print(
            "hydra usage reconcile codex: malformed server totals",
            file=sys.stderr,
        )
        return None
    return totals


def _reconcile_pass(
    chunks: list[list[dict[str, Any]]], *, apply: bool
) -> tuple[dict[str, int], bool]:
    totals = _empty_reconcile_totals()
    failed = False
    for chunk in chunks:
        response = _reconcile_request(chunk, apply=apply)
        if response is None:
            failed = True
            continue
        for key in _RECONCILE_TOTALS:
            totals[key] += response[key]
    return totals, failed


def _collect_reconcile_messages(base: Path) -> list[dict[str, Any]] | None:
    try:
        paths = sorted(base.rglob("rollout-*.jsonl"))
    except OSError as exc:
        print(f"hydra usage reconcile codex: cannot enumerate {base}: {exc}", file=sys.stderr)
        return None

    grouped: dict[str, list[dict[str, Any]]] = {}
    snapshots: dict[Path, tuple[int, int]] = {}
    for path in paths:
        try:
            before = path.stat()
        except OSError as exc:
            print(f"hydra usage reconcile codex: cannot stat {path}: {exc}", file=sys.stderr)
            return None
        result = parse_file(str(path), 0)
        try:
            after = path.stat()
        except OSError as exc:
            print(
                f"hydra usage reconcile codex: rollout disappeared {path}: {exc}",
                file=sys.stderr,
            )
            return None
        if (
            result.session_id is None
            or result.offset != before.st_size
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        ):
            print(
                f"hydra usage reconcile codex: rollout could not be read completely: {path}",
                file=sys.stderr,
            )
            return None
        snapshots[path] = (before.st_size, before.st_mtime_ns)
        if result.rows:
            grouped.setdefault(result.session_id, []).extend(result.rows)

    try:
        final_paths = sorted(base.rglob("rollout-*.jsonl"))
        stable = final_paths == paths and all(
            (stat.st_size, stat.st_mtime_ns) == snapshots[path]
            for path in paths
            for stat in (path.stat(),)
        )
    except OSError as exc:
        print(f"hydra usage reconcile codex: corpus changed while reading: {exc}", file=sys.stderr)
        return None
    if not stable:
        print(
            "hydra usage reconcile codex: corpus changed while reading; retry later",
            file=sys.stderr,
        )
        return None

    unique: dict[str, dict[str, Any]] = {}
    for session_id, session_messages in grouped.items():
        for row in session_messages:
            message = {**row, "session_id": session_id}
            existing = unique.get(row["message_id"])
            if existing is not None and existing != message:
                print(
                    "hydra usage reconcile codex: divergent duplicate message_id "
                    f"{row['message_id']}",
                    file=sys.stderr,
                )
                return None
            unique.setdefault(row["message_id"], message)
    return list(unique.values())


def _apply_reconcile_pass(
    chunks: list[list[dict[str, Any]]],
) -> tuple[dict[str, int], bool, str]:
    totals = _empty_reconcile_totals()
    for index, chunk in enumerate(chunks):
        response = _reconcile_request(chunk, apply=True)
        if response is None:
            phase = "partial apply" if index else "apply stopped"
            if index:
                print(
                    "hydra usage reconcile codex: partial apply - "
                    f"{index} of {len(chunks)} chunks completed; later state is unknown",
                    file=sys.stderr,
                )
            return totals, True, phase
        if response["conflicts"]:
            totals["conflicts"] += response["conflicts"]
            phase = "partial apply" if index else "apply stopped"
            if index:
                print(
                    "hydra usage reconcile codex: partial apply - "
                    f"{index} earlier chunks remain applied; remaining chunks were not sent",
                    file=sys.stderr,
                )
            return totals, True, phase
        for key in _RECONCILE_TOTALS:
            totals[key] += response[key]
    return totals, False, "applied"


def run_reconcile(root: str | None = None, *, apply: bool = False) -> int:
    base = Path(root) if root else Path.home() / ".codex" / "sessions"
    if not base.is_dir():
        print(
            f"hydra usage reconcile codex: no rollout root at {base}",
            file=sys.stderr,
        )
        _print_reconcile_totals(_empty_reconcile_totals(), "preview")
        return 1

    messages = _collect_reconcile_messages(base)
    if messages is None:
        _print_reconcile_totals(_empty_reconcile_totals(), "preview")
        return 1
    chunks = [messages[start : start + CHUNK] for start in range(0, len(messages), CHUNK)]

    preview, preview_failed = _reconcile_pass(chunks, apply=False)
    if not apply or preview_failed or preview["conflicts"]:
        _print_reconcile_totals(preview, "preview")
        return 1 if preview_failed or preview["conflicts"] else 0

    applied, apply_failed, apply_phase = _apply_reconcile_pass(chunks)
    _print_reconcile_totals(applied, apply_phase)
    return 1 if apply_failed or applied["conflicts"] else 0


def cmd_sweep(args: Any) -> int:
    return run_sweep(args.root, reset=args.reset)


def cmd_reconcile(args: Any) -> int:
    return run_reconcile(args.root, apply=args.apply)
