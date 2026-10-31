"""Session events to Hydra: the hook payload on stdin, POSTed to /api/hooks/event.

A command hook, not an HTTP hook, since 2026-10-31 (cursus #352): Claude Code
refuses an HTTP hook whose host resolves to a private address ("HTTP hook
blocked: ... resolves to 100.69.128.42 (private/link-local address)"), and Hydra
is reached over the tailnet (100.64.0.0/10) once its host has no public path.
Only loopback is exempt there; urllib has no such check.

Prints nothing: a command hook's stdout on SessionStart and UserPromptSubmit
lands in the session's context. A failed POST exits 1, Claude Code's non-blocking
hook error, as the HTTP hook showed one; never 2, which would block the prompt.
"""
from __future__ import annotations

import argparse
import sys

from hydra_cli import api
from hydra_cli.remote import read_hook_payload


def cmd_event(args: argparse.Namespace) -> None:
    payload = read_hook_payload()
    if not payload:
        sys.exit(0)
    try:
        status, body = api.post("/api/hooks/event", payload)
    except OSError as e:  # URLError, timeouts, refused connections
        print(f"hydra event: {e}", file=sys.stderr)
        sys.exit(1)
    if status != 200:
        print(f"hydra event: HTTP {status} {body[:200]}", file=sys.stderr)
        sys.exit(1)
