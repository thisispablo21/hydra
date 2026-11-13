"""A session's project must be pinned to its launch dir, not the live cwd.

Claude Code carries a Bash `cd` over to later tool calls and to hook processes
as long as it stays inside the project boundary, so `$PWD` at Stop time can be a
subdirectory. Auto-register then mints that subdir as its own project, and the
memory mirror path derived from it points at a dir Claude Code never writes to.
CLAUDE_PROJECT_DIR stays at the launch dir for the whole session, which is why
the hook and the CLI use it.

These tests read the shipped hook commands and drive the real CLI entry point
rather than asserting a hardcoded command string.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from hydra_cli.sync import cmd_sync

SETTINGS = Path(__file__).resolve().parent.parent / "client" / "settings.json"

# The hook runtime is `sh` on macOS/Linux and Git Bash on Windows; these expand
# the shipped command the POSIX way, so skip where no `sh` exists.
_SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(_SH is None, reason="POSIX sh required")

# `--cwd "<expr>"` out of every shipped hook command that runs `hydra_cli sync`.
_CWD_ARG = re.compile(r'--cwd\s+"([^"]+)"')


def sync_cwd_exprs() -> list[tuple[str, str]]:
    """(event, shell expr) for each wired `hydra_cli sync` hook command."""
    hooks = json.loads(SETTINGS.read_text())["hooks"]
    found = []
    for event, blocks in hooks.items():
        for block in blocks:
            for hook in block.get("hooks", []):
                cmd = hook.get("command", "")
                if "hydra_cli sync" not in cmd:
                    continue
                m = _CWD_ARG.search(cmd)
                assert m, f"{event} sync hook has no quoted --cwd: {cmd}"
                found.append((event, m.group(1)))
    assert found, "no hydra_cli sync hook commands found in client/settings.json"
    return found


def test_session_start_pulls_rendered_instructions():
    settings = SETTINGS.read_text()
    assert "python -m hydra_cli skills pull --harness claude-code" in settings
    assert "/api/config/skills/instructions" not in settings
    assert "/api/config/claude-md" not in settings


def test_memory_guard_is_wired_for_write_tools() -> None:
    groups = json.loads(SETTINGS.read_text())["hooks"]["PreToolUse"]
    guards = [
        group
        for group in groups
        if any(
            hook.get("command") == "python -m hydra_cli guard"
            for hook in group.get("hooks", [])
        )
    ]
    assert guards == [
        {
            "matcher": "Write|Edit|NotebookEdit|Bash",
            "hooks": [
                {
                    "type": "command",
                    "command": "python -m hydra_cli guard",
                    "timeout": 10,
                }
            ],
        }
    ]


def expand(expr: str, *, cwd: Path, env: dict[str, str]) -> str:
    """Expand a shell expression the way the hook runtime would."""
    assert _SH is not None  # guaranteed by pytestmark
    out = subprocess.run(
        [_SH, "-c", f'printf %s "{expr}"'],
        cwd=cwd, env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    """A launch dir and a subdir the model could have `cd`'d into."""
    launch = tmp_path / "proj"
    drifted = launch / "client"
    drifted.mkdir(parents=True)
    return launch, drifted


@pytest.mark.parametrize("event,expr", sync_cwd_exprs())
def test_hook_cwd_pins_to_launch_dir_despite_drift(
    event: str, expr: str, dirs: tuple[Path, Path]
) -> None:
    launch, drifted = dirs
    got = expand(expr, cwd=drifted, env={"CLAUDE_PROJECT_DIR": str(launch)})
    assert got == str(launch), (
        f"{event} hook resolved to {got!r}; a drifted $PWD must not win"
    )


@pytest.mark.parametrize("event,expr", sync_cwd_exprs())
def test_hook_cwd_falls_back_to_pwd_when_var_absent(
    event: str, expr: str, dirs: tuple[Path, Path]
) -> None:
    """Claude Code before v1.0.58 does not export CLAUDE_PROJECT_DIR."""
    _launch, drifted = dirs
    got = expand(expr, cwd=drifted, env={})
    assert got == str(drifted), f"{event} hook lost its $PWD fallback: {got!r}"


def _run_cmd_sync(monkeypatch: pytest.MonkeyPatch, **kw: object) -> str:
    """Drive the real CLI entry point; capture the cwd it hands run_sync."""
    seen: dict[str, str] = {}

    def fake_run_sync(cwd: str, **_: object) -> int:
        seen["cwd"] = cwd
        return 0

    monkeypatch.setattr("hydra_cli.sync.run_sync", fake_run_sync)
    args = argparse.Namespace(cwd=None, pull=True, dry_run=False)
    for k, v in kw.items():
        setattr(args, k, v)
    with pytest.raises(SystemExit) as exc:
        cmd_sync(args)
    assert exc.value.code == 0
    return seen["cwd"]


def test_cli_prefers_env_over_getcwd(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]
) -> None:
    """A hand-run `hydra sync` from a subdir still syncs the launch dir."""
    launch, drifted = dirs
    monkeypatch.chdir(drifted)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(launch))
    assert _run_cmd_sync(monkeypatch) == str(launch)


def test_cli_explicit_cwd_still_wins(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]
) -> None:
    launch, drifted = dirs
    monkeypatch.chdir(launch)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(launch))
    assert _run_cmd_sync(monkeypatch, cwd=str(drifted)) == str(drifted)


def test_cli_falls_back_to_getcwd(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]
) -> None:
    _launch, drifted = dirs
    monkeypatch.chdir(drifted)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert _run_cmd_sync(monkeypatch) == str(drifted)


def test_cli_refuses_a_cwd_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A path decoded from a transcript folder name (Claude Code writes `_` as
    `-`) auto-registered card-detection next to card_detection (2026-11-01)."""
    called: list[str] = []
    monkeypatch.setattr(
        "hydra_cli.sync.run_sync", lambda cwd, **_: called.append(cwd) or 0
    )
    args = argparse.Namespace(
        cwd=str(tmp_path / "card-detection"), pull=True, dry_run=False
    )
    with pytest.raises(SystemExit) as exc:
        cmd_sync(args)
    assert exc.value.code == 1
    assert called == []
