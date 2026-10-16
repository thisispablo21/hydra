"""Tests for the cross-harness memory write guard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from hydra_cli.guard import run_guard

ROOT = Path(__file__).resolve().parent.parent


def payload(tool_name: str, tool_input: dict[str, Any], *, cwd: Path | None = None) -> str:
    value: dict[str, Any] = {"tool_name": tool_name, "tool_input": tool_input}
    if cwd is not None:
        value["cwd"] = str(cwd)
    return json.dumps(value)


def memory_dir(home: Path) -> Path:
    path = home / ".claude" / "projects" / "proj" / "memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.mark.parametrize(
    "tool_name,path_key,filename",
    [
        ("Write", "file_path", "foo.md"),
        ("Edit", "file_path", "MEMORY.md"),
        ("NotebookEdit", "notebook_path", "notes.ipynb"),
    ],
)
def test_file_tools_deny_memory_targets(
    tmp_path: Path, tool_name: str, path_key: str, filename: str
) -> None:
    reason = run_guard(
        payload(tool_name, {path_key: str(memory_dir(tmp_path) / filename)}),
        {},
        home=tmp_path,
    )
    assert reason is not None
    assert "human-gated flow" in reason
    assert ("generated and rewritten" in reason) is (filename == "MEMORY.md")


def test_relative_file_path_resolves_against_payload_cwd(tmp_path: Path) -> None:
    memory = memory_dir(tmp_path)
    reason = run_guard(
        payload("Write", {"file_path": "memory/foo.md"}, cwd=memory.parent),
        {},
        home=tmp_path,
    )
    assert reason is not None


def test_custom_config_dir_is_guarded(tmp_path: Path) -> None:
    config = tmp_path / "config"
    target = config / "projects" / "proj" / "memory" / "foo.md"
    reason = run_guard(
        payload("Write", {"file_path": str(target)}),
        {"CLAUDE_CONFIG_DIR": str(config)},
        home=tmp_path,
    )
    assert reason is not None


def test_symlinked_home_compares_resolved_paths(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    target = memory_dir(real_home) / "foo.md"
    assert run_guard(
        payload("Write", {"file_path": str(target)}), {}, home=linked_home
    ) is not None


@pytest.mark.parametrize("operation", ["Update File", "Add File"])
def test_apply_patch_denies_memory_paths(tmp_path: Path, operation: str) -> None:
    target = memory_dir(tmp_path) / "foo.md"
    patch = f"*** Begin Patch\n*** {operation}: {target}\n+text\n*** End Patch"
    reason = run_guard(payload("apply_patch", {"command": patch}), {}, home=tmp_path)
    assert reason is not None


@pytest.mark.parametrize(
    "command",
    [
        "hydra memory create --name x",
        "python -m hydra_cli memory delete 7",
        "python3 -m hydra_cli memory update 3 --type user",
        "/opt/venv/bin/python -m hydra_cli memory create",
        "FOO=1 hydra memory create",
        "echo --flow | hydra memory delete 7",
        "hydra memory create; echo --flow",
        "(hydra memory create)",
        "true && hydra memory create --name x",
        "true\nhydra memory create --name x",
    ],
)
def test_bash_denies_unmarked_memory_writes(tmp_path: Path, command: str) -> None:
    reason = run_guard(payload("Bash", {"command": command}), {}, home=tmp_path)
    assert reason is not None


def test_bash_denies_ambiguous_unbalanced_quote(tmp_path: Path) -> None:
    reason = run_guard(
        payload("Bash", {"command": '"hydra memory create'}), {}, home=tmp_path
    )
    assert reason is not None
    assert reason.startswith("This command contains an ambiguous memory write.")


_SISYPHUS_NOTE = (
    "curl -K ~/.config/sisyphus/write.curlrc https://sisyphus.example/api/agent/todos/5"
    " --data-urlencode \"note@-\" <<'EOF'\n"
    "Bit again: the hydra memory global-skills-roadmap isn't where I left it.\n"
    "hydra memory delete 5\n"
    "EOF"
)


def test_bash_allows_a_heredoc_body_that_talks_about_memories(tmp_path: Path) -> None:
    """The 2026-10-16 refusal: a Sisyphus note whose BODY named a hydra memory and
    held an apostrophe. The body is data, and the apostrophe broke the parser."""
    assert run_guard(payload("Bash", {"command": _SISYPHUS_NOTE}), {}, home=tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        _SISYPHUS_NOTE + "\nhydra memory delete 5",
        "cat <<-X\n\tbody\n\tX\nhydra memory delete 5",
        "grep -c x <<< 'hydra memory' /tmp/f\nhydra memory delete 5",
    ],
)
def test_bash_still_denies_a_memory_write_after_a_heredoc(
    tmp_path: Path, command: str
) -> None:
    """Only the body is stripped: the terminator, a `<<-` body with tabs and a
    here-string are told apart, and the command after it is still read."""
    assert run_guard(payload("Bash", {"command": command}), {}, home=tmp_path) is not None


@pytest.mark.parametrize(
    "command",
    [
        "echo \"it's; hydra memory delete 5",
        "echo \"it's > ~/.claude/projects/proj/memory/foo.md",
        "echo \"it's; rm ~/.claude/projects/proj/memory/foo.md",
        "python -m hydra_cli memory delete 5 \\",
    ],
)
def test_bash_fallback_denies_writes_when_the_parser_gives_up(
    tmp_path: Path, command: str
) -> None:
    reason = run_guard(payload("Bash", {"command": command}), {}, home=tmp_path)
    assert reason is not None
    assert reason.startswith("This command contains an ambiguous memory write.")


@pytest.mark.parametrize(
    "command",
    [
        "echo \"it's; cat ~/.claude/projects/proj/memory/foo.md",
        "echo \"it's; hydra memory delete 5 --flow sync",
        "diff <(sort ~/.claude/projects/proj/memory/MEMORY.md) <(hydra memory list) \\",
        "echo \"it's; curl -d 'the hydra memory x' https://sisyphus.example/api",
    ],
)
def test_bash_fallback_allows_reads_and_marked_writes(tmp_path: Path, command: str) -> None:
    """A parse failure used to refuse on three substrings alone; now the same two
    checks run on a whitespace split, so a read or a marked write passes."""
    assert run_guard(payload("Bash", {"command": command}), {}, home=tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        "hydra memory create --flow sync --name x",
        "hydra memory create --flow=sync",
        "hydra memory list",
        "hydra memory get 3",
        "python -m hydra_cli sync --cwd .",
        "echo hydra memory",
        'git commit -m "memory: create hydra"',
        'grep "hydra memory create" notes.md',
        'bash -c "hydra memory create --name x"',
    ],
)
def test_bash_allows_reads_marked_writes_and_quoted_text(
    tmp_path: Path, command: str
) -> None:
    assert run_guard(payload("Bash", {"command": command}), {}, home=tmp_path) is None


def test_file_and_patch_rules_allow_non_memory_paths(tmp_path: Path) -> None:
    config = tmp_path / ".claude"
    outside = config / "projects" / "proj" / "notes.md"
    assert run_guard(
        payload("Write", {"file_path": str(tmp_path / "elsewhere.md")}),
        {},
        home=tmp_path,
    ) is None
    assert run_guard(
        payload("Write", {"file_path": str(outside)}), {}, home=tmp_path
    ) is None
    patch = f"*** Begin Patch\n*** Update File: {outside}\n+text\n*** End Patch"
    assert run_guard(
        payload("apply_patch", {"command": patch}), {}, home=tmp_path
    ) is None


@pytest.mark.parametrize(
    "stdin_text",
    [
        "not json",
        "[]",
        json.dumps({"tool_name": "Write"}),
        json.dumps({"tool_name": "Read", "tool_input": {}}),
        json.dumps({"tool_name": "Agent", "tool_input": {}}),
    ],
)
def test_invalid_and_ignored_payloads_fail_open(tmp_path: Path, stdin_text: str) -> None:
    assert run_guard(stdin_text, {}, home=tmp_path) is None


def test_flow_hint_is_appended_verbatim(tmp_path: Path) -> None:
    command = payload("Bash", {"command": "hydra memory delete 7"})
    plain = run_guard(command, {}, home=tmp_path)
    hinted = run_guard(command, {"HYDRA_FLOW_HINT": "Use /review now."}, home=tmp_path)
    assert plain is not None and "Use /review now." not in plain
    assert hinted is not None and hinted.endswith("\n\nUse /review now.")


def test_module_entry_emits_denial_json_and_fails_open_on_garbage(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "client"), "HOME": str(tmp_path)}
    denied = subprocess.run(
        [sys.executable, "-m", "hydra_cli", "guard"],
        input=payload("Bash", {"command": "hydra memory create --name x"}),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert denied.returncode == 0
    output = json.loads(denied.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"

    garbage = subprocess.run(
        [sys.executable, "-m", "hydra_cli", "guard"],
        input="not json",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert garbage.returncode == 0
    assert garbage.stdout == ""


@pytest.mark.parametrize(
    "template",
    [
        "echo hi > {path}",
        "printf x >> {path}",
        "rm {path}",
        "cp /tmp/x {path}",
        "tee {path}",
        "mv /tmp/x {path}",
        "sed -i s/a/b/ {path}",
        "cat /tmp/x | tee {path}",
        "true && rm {path}",
    ],
)
def test_bash_denies_shell_writes_to_the_mirror(tmp_path: Path, template: str) -> None:
    """Write/Edit are not the only way to reach a mirror file - an agent told to
    prefer shell edits reaches it through redirects and ordinary tools."""
    target = memory_dir(tmp_path) / "foo.md"
    reason = run_guard(
        payload("Bash", {"command": template.format(path=target)}), {}, home=tmp_path
    )
    assert reason is not None, template
    assert "human-gated flow" in reason


def test_bash_denies_tilde_paths(tmp_path: Path) -> None:
    """shlex does not expand ~, and the tilde form is how the path is usually typed."""
    memory_dir(tmp_path)
    reason = run_guard(
        payload("Bash", {"command": "echo x > ~/.claude/projects/proj/memory/foo.md"}),
        {},
        home=tmp_path,
    )
    assert reason is not None


def test_bash_denies_relative_paths_against_payload_cwd(tmp_path: Path) -> None:
    target = memory_dir(tmp_path)
    reason = run_guard(
        payload("Bash", {"command": "rm ./foo.md"}, cwd=target), {}, home=tmp_path
    )
    assert reason is not None


@pytest.mark.parametrize(
    "template",
    [
        "cat {path}",
        "grep -n flow {path}",
        "head -20 {path}",
        "ls {dir}",
        "wc -l {path}",
        "sed -n 1,5p {path}",
        "diff {path} /tmp/other.md",
    ],
)
def test_bash_still_allows_reads_of_the_mirror(tmp_path: Path, template: str) -> None:
    """The guard blocks writes, not inspection - reading a memory is normal work."""
    directory = memory_dir(tmp_path)
    command = template.format(path=directory / "foo.md", dir=directory)
    assert run_guard(payload("Bash", {"command": command}), {}, home=tmp_path) is None


def test_bash_allows_writes_outside_the_mirror(tmp_path: Path) -> None:
    outside = tmp_path / "notes.md"
    assert (
        run_guard(payload("Bash", {"command": f"echo x > {outside}"}), {}, home=tmp_path)
        is None
    )


def test_bash_denies_a_read_command_redirected_into_the_mirror(tmp_path: Path) -> None:
    """A reader is only a reader until its output is pointed at a mirror file."""
    target = memory_dir(tmp_path) / "foo.md"
    reason = run_guard(
        payload("Bash", {"command": f"cat /tmp/x > {target}"}), {}, home=tmp_path
    )
    assert reason is not None
