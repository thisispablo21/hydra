"""Fail-open guard for Hydra memory writes."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_FILE_TOOLS = {"Write", "Edit", "NotebookEdit"}
_MEMORY_VERBS = {"create", "update", "delete"}
_SEPARATORS = frozenset("|;&()")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
_PATCH_PATH = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$",
    re.MULTILINE,
)

# Commands that only read. Anything else touching a mirror path is treated as a
# write, because enumerating every mutating tool is a losing game.
_READ_COMMANDS = frozenset(
    {
        "awk", "cat", "diff", "du", "file", "find", "grep", "egrep", "fgrep",
        "head", "less", "ls", "more", "rg", "sed", "stat", "tail", "wc",
    }
)
_INPLACE = ("-i", "--in-place")
_REDIRECTS = frozenset({">", ">>", ">|", "<>"})

_GENERIC_REASON = (
    "Memory writes belong to a human-gated flow and are refused mid-session. "
    "The CLI accepts `--flow <name>` only from that flow. Park the fact in the "
    "session scratchpad and raise it at the end of the session."
)
_INDEX_PREFIX = "MEMORY.md is generated and rewritten on every pull. "
_AMBIGUOUS_PREFIX = "This command contains an ambiguous memory write. "


def _reason(env: Mapping[str, str], prefix: str = "") -> str:
    reason = f"{prefix}{_GENERIC_REASON}"
    if "HYDRA_FLOW_HINT" in env:
        reason += f"\n\n{env['HYDRA_FLOW_HINT']}"
    return reason


def _cwd(payload: dict[str, Any]) -> Path:
    cwd = payload.get("cwd")
    return Path(cwd) if isinstance(cwd, str) and cwd else Path(os.getcwd())


def _resolve_path(raw: str, cwd: Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def _expand(raw: str, cwd: Path, home: Path) -> Path | None:
    """Resolve a shell word to a path. shlex does not expand ~, and the tilde form
    is how a mirror path is usually typed, so it is expanded against `home`."""
    if raw == "~":
        raw = str(home)
    elif raw.startswith("~/"):
        raw = str(home) + raw[1:]
    try:
        return _resolve_path(raw, cwd)
    except (OSError, ValueError):
        return None


def _is_memory_target(path: Path, env: Mapping[str, str], home: Path) -> bool:
    config_value = env.get("CLAUDE_CONFIG_DIR")
    config = Path(config_value) if config_value else home / ".claude"
    projects = (config / "projects").resolve()
    try:
        relative = path.relative_to(projects)
    except ValueError:
        return False
    return len(relative.parts) >= 3 and relative.parts[1] == "memory"


def _split_newlines(command: str) -> str:
    """Turn shell newlines outside quotes into command separators."""
    out: list[str] = []
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            out.append(char)
            escaped = False
            continue
        if char == "\\" and quote != "'":
            out.append(char)
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            out.append(char)
            continue
        out.append(";" if char == "\n" and quote is None else char)
    return "".join(out)


def _simple_commands(command: str) -> list[list[str]]:
    lex = shlex.shlex(_split_newlines(command), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    commands: list[list[str]] = [[]]
    for token in lex:
        if token and all(char in _SEPARATORS for char in token):
            commands.append([])
        else:
            commands[-1].append(token.strip())
    return [words for words in commands if words]


def _basename(word: str) -> str:
    return word.replace("\\", "/").rsplit("/", 1)[-1]


def _is_memory_command(words: list[str]) -> bool:
    while words and _ASSIGNMENT.fullmatch(words[0]):
        words = words[1:]
    if any(word == "--flow" or word.startswith("--flow=") for word in words):
        return False

    module_invocation = any(
        words[index : index + 2] == ["-m", "hydra_cli"]
        for index in range(len(words) - 1)
    )
    for index in range(len(words) - 1):
        if words[index] != "memory" or words[index + 1] not in _MEMORY_VERBS:
            continue
        if index > 0 and _basename(words[index - 1]) == "hydra":
            return True
        if module_invocation:
            return True
    return False


def _writes_memory_path(
    words: list[str], env: Mapping[str, str], cwd: Path, home: Path
) -> bool:
    """Best-effort: a mirror path as an operand of anything but a reader, or as a
    redirect target of anything at all. A path hidden inside a quoted program
    string (`python -c "open(...)"`) is one token and stays invisible - this is a
    tripwire against drift, not a sandbox."""
    while words and _ASSIGNMENT.fullmatch(words[0]):
        words = words[1:]
    if not words:
        return False

    for index, word in enumerate(words):
        if word in _REDIRECTS and index + 1 < len(words):
            target = _expand(words[index + 1], cwd, home)
            if target is not None and _is_memory_target(target, env, home):
                return True

    name = _basename(words[0])
    in_place = name == "sed" and any(w.startswith(_INPLACE) for w in words[1:])
    if name in _READ_COMMANDS and not in_place:
        return False

    for word in words[1:]:
        if word in _REDIRECTS or word.startswith("-"):
            continue
        target = _expand(word, cwd, home)
        if target is not None and _is_memory_target(target, env, home):
            return True
    return False


_HEREDOC = re.compile(
    r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2(?=[\s;|&)]|$)"
)
_CRUDE_SEPARATOR = re.compile(r"\n|;|&&|\|\||[|&]")
_CRUDE_REDIRECT = re.compile(r">>|>")


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies before lexing. A body is data the shell never lexes,
    and an apostrophe inside one is what made shlex give up in every refusal so
    far (2026-09-09, 2026-10-15, 2026-10-16: notes sent to Sisyphus that merely
    mentioned a hydra memory). The `<<WORD` token stays; the terminator goes."""
    lines = command.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in _HEREDOC.finditer(line):
            strip_tabs, terminator = match.group(1), match.group(3)
            while index < len(lines):
                body_line = lines[index]
                index += 1
                if (body_line.lstrip("\t") if strip_tabs else body_line) == terminator:
                    break
    return "\n".join(kept)


def _crude_commands(command: str) -> list[list[str]]:
    """Whitespace lexer for a command shlex refused (an unbalanced quote outside
    any heredoc, a trailing backslash). Quotes are stripped from word edges but
    nothing is unquoted, so the same two checks run over the same words a bare
    command would produce, and a path inside a program string stays one word."""
    commands: list[list[str]] = []
    for segment in _CRUDE_SEPARATOR.split(command):
        padded = _CRUDE_REDIRECT.sub(r" \g<0> ", segment)
        words = [word for word in (raw.strip("'\"") for raw in padded.split()) if word]
        if words:
            commands.append(words)
    return commands


def _guard_bash(
    command: str, env: Mapping[str, str], *, cwd: Path, home: Path
) -> str | None:
    stripped = _strip_heredocs(command)
    try:
        commands, prefix = _simple_commands(stripped), ""
    except ValueError:
        commands, prefix = _crude_commands(stripped), _AMBIGUOUS_PREFIX
    if any(_is_memory_command(words) for words in commands):
        return _reason(env, prefix)
    if any(_writes_memory_path(words, env, cwd, home) for words in commands):
        return _reason(env, prefix)
    return None


def run_guard(stdin_text: str, env: Mapping[str, str], *, home: Path) -> str | None:
    """Return a denial reason for guarded memory writes, otherwise None."""
    try:
        payload = json.loads(stdin_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or "tool_input" not in payload:
        return None
    tool_input = payload["tool_input"]
    tool_name = payload.get("tool_name")
    if not isinstance(tool_input, dict) or not isinstance(tool_name, str):
        return None

    cwd = _cwd(payload)
    if tool_name in _FILE_TOOLS:
        key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
        raw_path = tool_input.get(key)
        if not isinstance(raw_path, str):
            return None
        target = _resolve_path(raw_path, cwd)
        if not _is_memory_target(target, env, home.resolve()):
            return None
        prefix = _INDEX_PREFIX if target.name == "MEMORY.md" else ""
        return _reason(env, prefix)

    if tool_name == "apply_patch":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return None
        for match in _PATCH_PATH.finditer(command):
            target = _resolve_path(match.group(1).strip(), cwd)
            if _is_memory_target(target, env, home.resolve()):
                return _reason(env)
        return None

    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return None
        return _guard_bash(command, env, cwd=cwd, home=home.resolve())
    return None


def main() -> int:
    try:
        reason = run_guard(sys.stdin.read(), os.environ, home=Path.home())
        if reason is not None:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": reason,
                        }
                    }
                )
            )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    main()
