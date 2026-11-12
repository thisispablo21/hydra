"""Tests for `hydra_cli event`, the command hook that reports session events.

It replaced the HTTP hooks on 2026-10-31: Claude Code blocks an HTTP hook whose
host resolves to a private address, and Hydra moved onto the tailnet. stdout must
stay empty, since SessionStart and UserPromptSubmit put it into the session.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from hydra_cli import event

TEMPLATE = Path(__file__).parent.parent / "client" / "settings.json"
PAYLOAD = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "cwd": "/tmp"}


def _run(monkeypatch: pytest.MonkeyPatch, stdin: str, post) -> int | str:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    with mock.patch.object(event.api, "post", post), pytest.raises(SystemExit) as exit_:
        event.cmd_event(argparse.Namespace())
        raise SystemExit(0)
    return exit_.value.code or 0


def test_posts_the_payload_and_prints_nothing(monkeypatch, capsys):
    post = mock.Mock(return_value=(200, "{}"))
    assert _run(monkeypatch, json.dumps(PAYLOAD), post) == 0
    post.assert_called_once_with("/api/hooks/event", PAYLOAD)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "post",
    [
        mock.Mock(return_value=(401, '{"detail":"bad token"}')),
        mock.Mock(side_effect=OSError("connection refused")),
    ],
)
def test_a_failed_post_is_a_non_blocking_error(monkeypatch, capsys, post):
    assert _run(monkeypatch, json.dumps(PAYLOAD), post) == 1  # never 2: that blocks
    out = capsys.readouterr()
    assert out.out == "" and out.err.startswith("hydra event: ")


def test_the_hook_command_line_reaches_it(monkeypatch, capsys):
    """`python -m hydra_cli event` as the template runs it, through main()'s dispatch
    (2026-10-31: a missing leaf_groups entry printed the help and exited 1)."""
    from hydra_cli.__main__ import main

    monkeypatch.setattr("sys.argv", ["hydra", "event"])
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(PAYLOAD).encode())))
    post = mock.Mock(return_value=(200, "{}"))
    with mock.patch.object(event.api, "post", post):
        main()
    post.assert_called_once_with("/api/hooks/event", PAYLOAD)
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(sys.platform == "win32", reason="the wiring runs in sh")
@pytest.mark.parametrize(("cli_exit", "hook_exit"), [(0, 0), (1, 1), (2, 1)])
def test_the_wiring_never_exits_2(tmp_path, cli_exit, hook_exit):
    """2026-11-11: a pre-`event` checkout in the live clone made argparse exit 2,
    Claude Code's blocking code; the wiring turns every failure into 1."""
    fake = tmp_path / "python"
    fake.write_text(f"#!/bin/sh\nexit {cli_exit}\n")
    fake.chmod(0o755)
    hooks = json.loads(TEMPLATE.read_text(encoding="utf-8"))["hooks"]
    command = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    assert subprocess.run(["sh", "-c", command], env=env).returncode == hook_exit


def test_no_payload_sends_nothing(monkeypatch):
    post = mock.Mock()
    assert _run(monkeypatch, "", post) == 0
    post.assert_not_called()


def test_template_reports_events_by_command_never_http():
    hooks = json.loads(TEMPLATE.read_text(encoding="utf-8"))["hooks"]
    entries = [h for groups in hooks.values() for g in groups for h in g["hooks"]]
    assert all(h["type"] != "http" for h in entries)
    reporting = {
        name for name, groups in hooks.items()
        if any(h.get("command") == "python -m hydra_cli event || exit 1"
               for g in groups for h in g["hooks"])
    }
    assert reporting == {
        "SessionStart", "SessionEnd", "UserPromptSubmit", "PostToolUse", "Stop",
        "SubagentStart", "SubagentStop", "Notification",
    }
