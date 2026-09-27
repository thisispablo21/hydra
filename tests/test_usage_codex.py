"""Codex rollout parsing and sweep tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from hydra_cli import usage_codex
from hydra_cli.__main__ import build_parser

from server.models import UsageBatch

FIXTURES = Path(__file__).parent / "fixtures"
PARENT = "11111111-1111-4111-8111-111111111111"
GUARDIAN = "22222222-2222-4222-8222-222222222222"
REVIEW = "33333333-3333-4333-8333-333333333333"
SPAWN = "44444444-4444-4444-8444-444444444444"
TURNLESS = "55555555-5555-4555-8555-555555555555"


def _fixture(name: str) -> Path:
    return FIXTURES / f"codex_{name}.jsonl"


def _install(root: Path, name: str, thread_id: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
    shutil.copyfile(_fixture(name), path)
    return path


def _record_rollout(thread_id: str, session_id: str, count: int) -> str:
    records = [
        {
            "timestamp": "2026-02-01T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "session_id": session_id,
                "parent_thread_id": None,
                "source": "exec",
                "cwd": "/project",
            },
        },
        {
            "timestamp": "2026-02-01T00:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "effort": "high", "cwd": "/project"},
        },
    ]
    for i in range(1, count + 1):
        usage = {
            "input_tokens": i,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": i,
        }
        last = {
            "input_tokens": 1,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 1,
        }
        records.append(
            {
                "timestamp": f"2026-02-01T00:{i // 60:02d}:{i % 60:02d}Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": usage,
                        "last_token_usage": last,
                        "model_context_window": 258400,
                    },
                },
            }
        )
    return "".join(json.dumps(record) + "\n" for record in records)


def _inherited_usage_rollout(
    path: Path, *, first_last: dict[str, int] | None
) -> None:
    def usage(
        input_tokens: int, cached_input_tokens: int, output_tokens: int, total_tokens: int
    ) -> dict[str, int]:
        return {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "cache_write_input_tokens": 0,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": 0,
            "total_tokens": total_tokens,
        }

    first_info = {
        "total_token_usage": usage(100, 40, 10, 110),
        "model_context_window": 258400,
    }
    if first_last is not None:
        first_info["last_token_usage"] = first_last
    records = [
        {
            "timestamp": "2026-02-01T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": SPAWN,
                "session_id": PARENT,
                "parent_thread_id": PARENT,
                "source": {"subagent": {"thread_spawn": {"parent": PARENT}}},
                "cwd": "/project",
            },
        },
        {
            "timestamp": "2026-02-01T00:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "effort": "high", "cwd": "/project"},
        },
        {
            "timestamp": "2026-02-01T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": first_info},
        },
        {
            "timestamp": "2026-02-01T00:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": usage(107, 42, 13, 120),
                    "last_token_usage": usage(3, 1, 1, 4),
                    "model_context_window": 258400,
                },
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


class FakeApi:
    def __init__(self, *, handshake: int = 200, posts: list[int] | None = None):
        self.handshake = handshake
        self.post_statuses = iter(posts or [])
        self.gets: list[str] = []
        self.batches: list[dict] = []

    def get(self, path: str) -> tuple[int, str]:
        self.gets.append(path)
        return self.handshake, "{}"

    def post(self, path: str, payload: dict) -> tuple[int, str]:
        assert path == "/api/usage/messages"
        self.batches.append(payload)
        return next(self.post_statuses, 200), "response"


@pytest.fixture
def sweep_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "sessions"
    state = tmp_path / "state"
    fake = FakeApi()
    monkeypatch.setattr(usage_codex, "state_dir", lambda: state)
    monkeypatch.setattr(usage_codex.api, "get", fake.get)
    monkeypatch.setattr(usage_codex.api, "post", fake.post)
    return root, state, fake


def test_parent_parser_diffs_duplicates_and_resets_deterministically():
    first = usage_codex.parse_file(str(_fixture("parent")))
    second = usage_codex.parse_file(str(_fixture("parent")))

    assert first.rows == second.rows
    assert len(first.rows) == 3
    assert len({row["message_id"] for row in first.rows}) == 3
    assert first.usage_events == 4
    assert first.rows[0]["input_tokens"] == 40
    assert first.rows[0]["cache_read_tokens"] == 60
    assert first.rows[1]["input_tokens"] == 20
    assert first.rows[2]["input_tokens"] == 20
    assert first.rows[2]["cache_read_tokens"] == 10
    assert first.rows[2]["output_tokens"] == 5


def test_resume_rebuilds_context_but_emits_only_rows_after_offset():
    path = _fixture("parent")
    lines = path.read_bytes().splitlines(keepends=True)
    offset = sum(len(line) for line in lines[:4])
    cold = usage_codex.parse_file(str(path))
    resumed = usage_codex.parse_file(str(path), offset)

    assert [row["message_id"] for row in resumed.rows] == [
        row["message_id"] for row in cold.rows[1:]
    ]
    assert resumed.offset == cold.offset


def test_resume_reset_uses_last_snapshot_then_continues_differencing():
    parsed = usage_codex.parse_file(str(_fixture("resume_reset")))

    assert [row["input_tokens"] for row in parsed.rows] == [6125, 373, 216]
    assert [row["cache_read_tokens"] for row in parsed.rows] == [11136, 17024, 17280]
    assert [row["output_tokens"] for row in parsed.rows] == [9, 78, 22]
    assert len({row["message_id"] for row in parsed.rows}) == 3
    assert all(row["service_tier"] == "priority" for row in parsed.rows)
    UsageBatch.model_validate({"session_id": parsed.session_id, "messages": parsed.rows})


def test_resume_reset_offset_replay_emits_only_post_reset_usage():
    path = _fixture("resume_reset")
    lines = path.read_bytes().splitlines(keepends=True)
    offset = sum(len(line) for line in lines[:4])

    resumed = usage_codex.parse_file(str(path), offset)

    assert [row["input_tokens"] for row in resumed.rows] == [373, 216]
    assert [row["output_tokens"] for row in resumed.rows] == [78, 22]
    assert len({row["message_id"] for row in resumed.rows}) == 2
    assert resumed.offset == path.stat().st_size


def test_guardian_keeps_auto_review_alias_unpriced():
    row = usage_codex.parse_file(str(_fixture("guardian"))).rows[0]

    assert row["model"] == "codex-auto-review"
    assert row["is_subagent"] is True
    assert row["agent_type"] == "guardian"
    assert row["cache_write_5m_tokens"] == 3


@pytest.mark.parametrize(
    "name,agent_type,thread_id",
    [("review", "review", REVIEW), ("spawn", "spawn", SPAWN)],
)
def test_child_types_and_second_session_meta(name: str, agent_type: str, thread_id: str):
    parsed = usage_codex.parse_file(str(_fixture(name)))
    assert parsed.session_id == PARENT
    assert parsed.rows[0]["is_subagent"] is True
    assert parsed.rows[0]["agent_type"] == agent_type
    assert parsed.rows[0]["message_id"].startswith(f"codex:{thread_id}:")


@pytest.mark.parametrize(
    "first_last,expected",
    [
        (
            {
                "input_tokens": 4,
                "cached_input_tokens": 1,
                "cache_write_input_tokens": 0,
                "output_tokens": 2,
                "reasoning_output_tokens": 0,
                "total_tokens": 6,
            },
            [(3, 1, 2), (5, 2, 3)],
        ),
        (
            {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 0,
            },
            [(5, 2, 3)],
        ),
    ],
)
def test_spawn_first_snapshot_uses_only_last_call(
    tmp_path: Path,
    first_last: dict[str, int],
    expected: list[tuple[int, int, int]],
):
    path = tmp_path / f"rollout-2026-02-01T00-00-00-{SPAWN}.jsonl"
    _inherited_usage_rollout(path, first_last=first_last)

    parsed = usage_codex.parse_file(str(path))

    assert [
        (row["input_tokens"], row["cache_read_tokens"], row["output_tokens"])
        for row in parsed.rows
    ] == expected
    assert all(row["agent_type"] == "spawn" for row in parsed.rows)
    assert parsed.ambiguous_first_usage == 0


def test_missing_first_last_usage_sets_baseline_and_reports_ambiguity(
    sweep_env, capsys
):
    root, _state, fake = sweep_env
    root.mkdir()
    path = root / f"rollout-2026-02-01T00-00-00-{SPAWN}.jsonl"
    _inherited_usage_rollout(path, first_last=None)

    parsed = usage_codex.parse_file(str(path))
    assert len(parsed.rows) == 1
    assert parsed.rows[0]["input_tokens"] == 5
    assert parsed.rows[0]["cache_read_tokens"] == 2
    assert parsed.rows[0]["output_tokens"] == 3
    assert parsed.ambiguous_first_usage == 1

    assert usage_codex.run_sweep(str(root)) == 0
    assert sum(len(batch["messages"]) for batch in fake.batches) == 1
    assert "1 ambiguous first usage events" in capsys.readouterr().err

    fake.batches.clear()
    assert usage_codex.run_sweep(str(root)) == 0
    assert fake.batches == []
    assert "0 ambiguous first usage events" in capsys.readouterr().err


def test_turnless_exec_fixture_has_no_usage():
    parsed = usage_codex.parse_file(str(_fixture("turnless_exec")))
    assert parsed.rows == []
    assert parsed.usage_events == 0


def test_turnless_exec_sweep_reports_file_without_post(sweep_env, capsys):
    root, _state, fake = sweep_env
    _install(root, "turnless_exec", TURNLESS)

    assert usage_codex.run_sweep(str(root)) == 0
    assert "1 files with no usage events" in capsys.readouterr().err
    assert fake.batches == []


def test_events_before_turn_are_skipped_and_long_context_is_counted(tmp_path: Path):
    thread_id = "88888888-8888-4888-8888-888888888888"
    path = tmp_path / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
    usage_a = {
        "input_tokens": 5,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 5,
    }
    usage_b = dict(usage_a, input_tokens=6, total_tokens=6)
    records = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "session_id": thread_id, "cwd": "/project"},
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": usage_a, "last_token_usage": usage_a},
            },
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/project"},
        },
        {
            "timestamp": "2026-01-01T00:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": usage_b,
                    "last_token_usage": usage_b,
                    "model_context_window": 300000,
                },
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))

    parsed = usage_codex.parse_file(str(path))
    assert len(parsed.rows) == 1
    assert parsed.skipped_without_turn == 1
    assert parsed.long_context_calls == 1


def test_corpus_canary_uses_independent_literal_denominator():
    fixtures = sorted(FIXTURES.glob("codex_*.jsonl"))
    literal_events = sum(
        path.read_bytes().count(b'"type":"token_count"') for path in fixtures
    )
    parser_events = sum(usage_codex.parse_file(str(path)).usage_events for path in fixtures)
    assert parser_events == literal_events == 12

    parent = usage_codex.parse_file(str(_fixture("parent"))).rows
    billed_tokens = sum(
        row["input_tokens"] + row["cache_read_tokens"] + row["output_tokens"]
        for row in parent
    )
    assert billed_tokens == 180 + 35


def test_partial_line_is_re_read_on_next_sweep(sweep_env):
    root, _state, fake = sweep_env
    path = _install(root, "parent", PARENT)
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:3]) + lines[4][:40])

    assert usage_codex.run_sweep(str(root)) == 0
    assert sum(len(batch["messages"]) for batch in fake.batches) == 1

    path.write_bytes(b"".join(lines))
    fake.batches.clear()
    assert usage_codex.run_sweep(str(root)) == 0
    assert sum(len(batch["messages"]) for batch in fake.batches) == 2


def test_truncated_rollout_restarts_at_zero(sweep_env):
    root, _state, fake = sweep_env
    path = _install(root, "parent", PARENT)
    assert usage_codex.run_sweep(str(root)) == 0

    path.write_text(_record_rollout(PARENT, PARENT, 1), encoding="utf-8")
    fake.batches.clear()
    assert usage_codex.run_sweep(str(root)) == 0
    assert sum(len(batch["messages"]) for batch in fake.batches) == 1


def test_unchanged_rollout_is_not_parsed(sweep_env, monkeypatch: pytest.MonkeyPatch):
    root, _state, fake = sweep_env
    _install(root, "parent", PARENT)
    assert usage_codex.run_sweep(str(root)) == 0
    fake.batches.clear()

    def fail(*_args, **_kwargs):
        raise AssertionError("unchanged file was opened")

    monkeypatch.setattr(usage_codex, "parse_file", fail)
    assert usage_codex.run_sweep(str(root)) == 0
    assert fake.batches == []


def test_handshake_failure_exits_zero_without_post_or_state_change(
    sweep_env, monkeypatch: pytest.MonkeyPatch
):
    root, state, _fake = sweep_env
    _install(root, "parent", PARENT)
    state.mkdir()
    state_path = state / "codex-sweep.json"
    state_path.write_bytes(b'{"old": 3}\n')
    before = state_path.read_bytes()
    fake = FakeApi(handshake=422)
    monkeypatch.setattr(usage_codex.api, "get", fake.get)
    monkeypatch.setattr(usage_codex.api, "post", fake.post)

    assert usage_codex.run_sweep(str(root), reset=True) == 0
    assert len(fake.gets) == 1
    assert "group_by=harness" in fake.gets[0]
    assert fake.batches == []
    assert state_path.read_bytes() == before


@pytest.mark.parametrize("statuses", [[500], [200, 500]])
def test_post_failure_leaves_state_untouched(
    sweep_env, monkeypatch: pytest.MonkeyPatch, statuses: list[int]
):
    root, state, _fake = sweep_env
    root.mkdir()
    path = root / (
        "rollout-2026-02-01T00-00-00-66666666-6666-4666-8666-666666666666.jsonl"
    )
    count = 501 if len(statuses) == 2 else 1
    path.write_text(
        _record_rollout(
            "66666666-6666-4666-8666-666666666666",
            "66666666-6666-4666-8666-666666666666",
            count,
        ),
        encoding="utf-8",
    )
    state.mkdir()
    state_path = state / "codex-sweep.json"
    state_path.write_bytes(b'{"old": 3}\n')
    before = state_path.read_bytes()
    fake = FakeApi(posts=statuses)
    monkeypatch.setattr(usage_codex.api, "get", fake.get)
    monkeypatch.setattr(usage_codex.api, "post", fake.post)

    assert usage_codex.run_sweep(str(root), reset=True) == 1
    assert len(fake.batches) == len(statuses)
    assert state_path.read_bytes() == before


def test_unparseable_state_triggers_full_rescan(sweep_env):
    root, state, fake = sweep_env
    _install(root, "parent", PARENT)
    state.mkdir()
    (state / "codex-sweep.json").write_text("{truncated", encoding="utf-8")

    assert usage_codex.run_sweep(str(root)) == 0
    assert sum(len(batch["messages"]) for batch in fake.batches) == 3


def test_batches_are_chunked_and_grouped_by_root_session(
    sweep_env, monkeypatch: pytest.MonkeyPatch
):
    root, _state, fake = sweep_env
    root.mkdir()
    first = "66666666-6666-4666-8666-666666666666"
    second = "77777777-7777-4777-8777-777777777777"
    (root / f"rollout-2026-02-01T00-00-00-{first}.jsonl").write_text(
        _record_rollout(first, first, 501), encoding="utf-8"
    )
    (root / f"rollout-2026-02-01T00-00-00-{second}.jsonl").write_text(
        _record_rollout(second, second, 1), encoding="utf-8"
    )
    guardian = _install(root, "guardian", GUARDIAN)
    guardian.write_text(
        guardian.read_text().replace(PARENT, first), encoding="utf-8"
    )
    monkeypatch.setattr(usage_codex, "CHUNK", 500)

    assert usage_codex.run_sweep(str(root)) == 0
    sizes = sorted(len(batch["messages"]) for batch in fake.batches)
    assert sizes == [1, 2, 500]
    for batch in fake.batches:
        expected_threads = {first, GUARDIAN} if batch["session_id"] == first else {second}
        assert all(
            message["message_id"].split(":", 2)[1] in expected_threads
            for message in batch["messages"]
        )


def _tier_rollout(tmp_path: Path, thread_id: str, tier: str | None) -> Path:
    """Rollout whose settings event lands AFTER a priced turn, as Codex writes it."""
    path = tmp_path / f"rollout-2026-09-05T00-00-00-{thread_id}.jsonl"
    def usage(n: int) -> dict[str, int]:
        return {
            "input_tokens": n,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": n,
        }
    records: list[dict[str, object]] = [
        {
            "timestamp": "2026-09-05T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "session_id": thread_id, "cwd": "/project"},
        },
        {
            "timestamp": "2026-09-05T00:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-6-astra", "cwd": "/project"},
        },
        {
            "timestamp": "2026-09-05T00:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": usage(10), "last_token_usage": usage(10)},
            },
        },
    ]
    if tier is not None:
        records.append({
            "timestamp": "2026-09-05T00:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "thread_settings_applied",
                "thread_id": thread_id,
                "thread_settings": {"model": "gpt-6-astra", "service_tier": tier},
            },
        })
    records.append({
        "timestamp": "2026-09-05T00:00:04Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": usage(30), "last_token_usage": usage(20)},
        },
    })
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_tier_applies_to_rows_recorded_before_the_settings_event(tmp_path: Path):
    # The settings event trails the first turn in real rollouts, but the tier is
    # constant per file, so the earlier row must be stamped too - not left base.
    path = _tier_rollout(tmp_path, "77777777-7777-4777-8777-777777777777", "priority")

    parsed = usage_codex.parse_file(str(path))

    assert len(parsed.rows) == 2
    assert [row["service_tier"] for row in parsed.rows] == ["priority", "priority"]


def test_rollout_without_a_settings_event_leaves_the_tier_absent(tmp_path: Path):
    path = _tier_rollout(tmp_path, "66666666-6666-4666-8666-666666666666", None)

    parsed = usage_codex.parse_file(str(path))

    assert len(parsed.rows) == 2
    assert all(row["service_tier"] is None for row in parsed.rows)


class ReconcileApi:
    def __init__(
        self,
        *,
        conflict=False,
        fail_apply_chunk: int | None = None,
        apply_conflict_chunk: int | None = None,
    ):
        self.conflict = conflict
        self.fail_apply_chunk = fail_apply_chunk
        self.apply_conflict_chunk = apply_conflict_chunk
        self.posts: list[dict] = []
        self.apply_calls = 0

    def post(self, path: str, payload: dict) -> tuple[int, str]:
        assert path == "/api/usage/reconcile/codex"
        self.posts.append(payload)
        if payload["apply"]:
            self.apply_calls += 1
            if self.fail_apply_chunk == self.apply_calls:
                return 500, "failed"
        preview_conflict = self.conflict and not payload["apply"] and len(self.posts) == 1
        apply_conflict = payload["apply"] and self.apply_conflict_chunk == self.apply_calls
        conflicts = int(preview_conflict or apply_conflict)
        body = {
            "inserted": len(payload["messages"]) - conflicts,
            "updated": 0,
            "unchanged": 0,
            "foreign_unchanged": 0,
            "conflicts": conflicts,
            "conflict_ids": [],
            "applied": payload["apply"] and not conflicts,
        }
        return 200, json.dumps(body)


def test_reconcile_parser_cli_shape():
    args = build_parser().parse_args(
        ["usage", "reconcile", "codex", "--root", "rollouts", "--apply"]
    )

    assert args.group == "usage"
    assert args.command == "reconcile"
    assert args.reconcile_harness == "codex"
    assert args.root == "rollouts"
    assert args.apply is True


def test_reconcile_dry_run_parses_from_zero_and_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "sessions"
    _install(root, "parent", PARENT)
    state = tmp_path / "state"
    state.mkdir()
    state_path = state / "codex-sweep.json"
    state_path.write_bytes(b'{"old": 3}\n')
    before = state_path.read_bytes()
    fake = ReconcileApi()
    original_parse = usage_codex.parse_file
    calls: list[int] = []

    def recording_parse(path, offset=0):
        calls.append(offset)
        return original_parse(path, offset)

    monkeypatch.setattr(usage_codex, "state_dir", lambda: state)
    monkeypatch.setattr(
        usage_codex,
        "_load_offsets",
        lambda: (_ for _ in ()).throw(AssertionError("reconcile loaded sweep offsets")),
    )
    monkeypatch.setattr(
        usage_codex,
        "_save_offsets",
        lambda _offsets: (_ for _ in ()).throw(AssertionError("reconcile saved sweep offsets")),
    )
    monkeypatch.setattr(usage_codex, "parse_file", recording_parse)
    monkeypatch.setattr(usage_codex.api, "post", fake.post)

    assert usage_codex.run_reconcile(str(root)) == 0
    assert calls and all(offset == 0 for offset in calls)
    assert fake.posts and all(post["apply"] is False for post in fake.posts)
    assert all(message["session_id"] == PARENT for message in fake.posts[0]["messages"])
    assert state_path.read_bytes() == before


def test_reconcile_apply_previews_every_chunk_before_applying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "sessions"
    root.mkdir()
    thread_id = "66666666-6666-4666-8666-666666666666"
    (root / f"rollout-2026-02-01T00-00-00-{thread_id}.jsonl").write_text(
        _record_rollout(thread_id, thread_id, 501), encoding="utf-8"
    )
    fake = ReconcileApi()
    monkeypatch.setattr(usage_codex.api, "post", fake.post)

    assert usage_codex.run_reconcile(str(root), apply=True) == 0
    assert [post["apply"] for post in fake.posts] == [False, False, True, True]
    assert [len(post["messages"]) for post in fake.posts] == [500, 1, 500, 1]


def test_reconcile_conflict_aborts_after_complete_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "sessions"
    root.mkdir()
    thread_id = "66666666-6666-4666-8666-666666666666"
    (root / f"rollout-2026-02-01T00-00-00-{thread_id}.jsonl").write_text(
        _record_rollout(thread_id, thread_id, 501), encoding="utf-8"
    )
    fake = ReconcileApi(conflict=True)
    monkeypatch.setattr(usage_codex.api, "post", fake.post)

    assert usage_codex.run_reconcile(str(root), apply=True) == 1
    assert [post["apply"] for post in fake.posts] == [False, False]


def test_reconcile_rejects_divergent_duplicate_ids_before_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "sessions"
    root.mkdir()
    ids = [
        "66666666-6666-4666-8666-666666666666",
        "77777777-7777-4777-8777-777777777777",
    ]
    paths = [root / f"rollout-2026-02-01T00-00-00-{thread_id}.jsonl" for thread_id in ids]
    for path in paths:
        path.write_text("{}\n", encoding="utf-8")

    def duplicate_parse(path, offset=0):
        del offset
        value = 1 if Path(path) == paths[0] else 2
        return usage_codex.ParseResult(
            [{"message_id": "codex:duplicate", "input_tokens": value}],
            Path(path).stat().st_size,
            "root",
        )

    posts = []
    monkeypatch.setattr(usage_codex, "parse_file", duplicate_parse)
    monkeypatch.setattr(usage_codex.api, "post", lambda path, payload: posts.append(payload))

    assert usage_codex.run_reconcile(str(root)) == 1
    assert posts == []


def test_reconcile_fails_closed_when_rollout_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "sessions"
    path = _install(root, "parent", PARENT)
    posts = []

    def disappearing_parse(path_str, offset=0):
        del offset
        path.unlink()
        return usage_codex.ParseResult([], 0, PARENT)

    monkeypatch.setattr(usage_codex, "parse_file", disappearing_parse)
    monkeypatch.setattr(usage_codex.api, "post", lambda path, payload: posts.append(payload))

    assert usage_codex.run_reconcile(str(root), apply=True) == 1
    assert posts == []


def test_reconcile_reports_partial_apply_on_later_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    root = tmp_path / "sessions"
    root.mkdir()
    thread_id = "66666666-6666-4666-8666-666666666666"
    (root / f"rollout-2026-02-01T00-00-00-{thread_id}.jsonl").write_text(
        _record_rollout(thread_id, thread_id, 501), encoding="utf-8"
    )
    fake = ReconcileApi(fail_apply_chunk=2)
    monkeypatch.setattr(usage_codex.api, "post", fake.post)

    assert usage_codex.run_reconcile(str(root), apply=True) == 1
    assert "partial apply" in capsys.readouterr().err
    assert [post["apply"] for post in fake.posts] == [False, False, True, True]


def test_reconcile_apply_conflict_excludes_rolled_back_chunk_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    root = tmp_path / "sessions"
    root.mkdir()
    thread_id = "66666666-6666-4666-8666-666666666666"
    (root / f"rollout-2026-02-01T00-00-00-{thread_id}.jsonl").write_text(
        _record_rollout(thread_id, thread_id, 502), encoding="utf-8"
    )
    fake = ReconcileApi(apply_conflict_chunk=2)
    monkeypatch.setattr(usage_codex.api, "post", fake.post)

    assert usage_codex.run_reconcile(str(root), apply=True) == 1
    captured = capsys.readouterr()
    assert "inserted 500" in captured.out
    assert "conflicts 1 (partial apply)" in captured.out
    assert "inserted 501" not in captured.out
    assert "(applied)" not in captured.out
    assert "partial apply" in captured.err
    assert [post["apply"] for post in fake.posts] == [False, False, True, True]


def test_reconcile_transport_failure_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    root = tmp_path / "sessions"
    _install(root, "parent", PARENT)

    def fail_post(_path, _payload):
        raise OSError("offline")

    monkeypatch.setattr(usage_codex.api, "post", fail_post)

    assert usage_codex.run_reconcile(str(root)) == 1
    assert "POST failed: offline" in capsys.readouterr().err
