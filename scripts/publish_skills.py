#!/usr/bin/env python3
"""Publish repo-authored skills to the Hydra server."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_METADATA_KEYS = {"enabled", "implicit_invocation", "instances"}


def _frontmatter_fields(text: str) -> dict[str, str] | None:
    # Kept local because server-only hosts do not install hydra_cli.
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    end = next(
        (index for index, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"),
        None,
    )
    if end is None:
        return None
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            fields[key.strip()] = value
    return fields


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name}: malformed JSON: {exc.msg}") from exc
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{path.name}: {exc}") from exc


def _metadata(skill_dir: Path) -> dict[str, Any]:
    path = skill_dir / "skill.json"
    if not path.exists():
        return {"enabled": True, "implicit_invocation": False, "instances": None}
    value = _read_json(path)
    if not isinstance(value, dict):
        raise ValueError("skill.json: expected a JSON object")
    unknown = sorted(set(value) - _METADATA_KEYS)
    if unknown:
        raise ValueError(f"skill.json: unknown key: {unknown[0]}")
    enabled = value.get("enabled", True)
    implicit = value.get("implicit_invocation", False)
    instances = value.get("instances")
    if not isinstance(enabled, bool):
        raise ValueError("skill.json: enabled must be a boolean")
    if not isinstance(implicit, bool):
        raise ValueError("skill.json: implicit_invocation must be a boolean")
    if instances is not None and (
        not isinstance(instances, list)
        or not all(isinstance(instance, str) for instance in instances)
    ):
        raise ValueError("skill.json: instances must be an array of strings or null")
    return {"enabled": enabled, "implicit_invocation": implicit, "instances": instances}


def _variants(skill_dir: Path) -> dict[str, dict[str, str]]:
    variants: dict[str, dict[str, str]] = {}
    for path in sorted(skill_dir.glob("*.json")):
        harness = path.stem
        if harness == "skill" or not _NAME_RE.fullmatch(harness):
            continue
        if harness == "common":
            raise ValueError("common.json: 'common' is not a legal harness")
        value = _read_json(path)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}: expected a JSON object")
        if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
            raise ValueError(f"{path.name}: slot names and values must be strings")
        variants[harness] = value
    return variants


def build_skill(skill_dir: Path) -> dict[str, Any]:
    """Validate one source directory and build its PUT body."""
    name = skill_dir.name
    if not _NAME_RE.fullmatch(name):
        raise ValueError("invalid skill name")
    common_path = skill_dir / "common.md"
    if not common_path.is_file():
        raise ValueError("common.md is required")
    try:
        common = common_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"common.md: {exc}") from exc
    kind = "instructions" if name == "instructions" else "skill"
    if kind == "skill":
        fields = _frontmatter_fields(common)
        if fields is None:
            raise ValueError("common.md must start with a closed frontmatter block")
        if fields.get("name") != name:
            raise ValueError(f"common.md frontmatter name must equal {name!r}")
        if not fields.get("description", "").strip():
            raise ValueError("common.md frontmatter description must be non-empty")
    return {
        "kind": kind,
        **_metadata(skill_dir),
        "common": common,
        "variants": _variants(skill_dir),
    }


def put_skill(url: str, token: str, name: str, body: dict[str, Any]) -> tuple[int, str]:
    """PUT one skill, returning the HTTP status and response body."""
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/config/skills/{name}",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # Same UA as hydra_cli/api.py: without one, Cloudflare answers 403 error 1010
            # on the public URL, so the publisher only worked from the Pi via loopback.
            "User-Agent": "hydra-cli/0.1",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def publish(source: Path, url: str, token: str) -> int:
    if not source.is_dir():
        print(f"publish_skills: source dir not found: {source}", file=sys.stderr)
        return 1
    skill_dirs = sorted(path for path in source.iterdir() if path.is_dir())
    if not skill_dirs:
        print(f"publish_skills: no skill dirs in {source}")
        return 0

    skills: list[tuple[str, dict[str, Any]]] = []
    failed_validation = False
    for skill_dir in skill_dirs:
        try:
            skills.append((skill_dir.name, build_skill(skill_dir)))
        except ValueError as exc:
            failed_validation = True
            print(f"publish_skills: {skill_dir}: {exc}", file=sys.stderr)
    if failed_validation:
        return 1

    if not token:
        print(
            "publish_skills: WARNING: HYDRA_AUTH_TOKEN is empty; PUTs will 401 "
            "unless the server runs with HYDRA_ALLOW_NO_AUTH=1.",
            file=sys.stderr,
        )

    published = 0
    failed_publish = False
    for name, body in skills:
        try:
            status, response_body = put_skill(url, token, name, body)
        except OSError as exc:
            failed_publish = True
            print(f"publish_skills: {name}: {exc}", file=sys.stderr)
            continue
        if not 200 <= status < 300:
            failed_publish = True
            print(f"publish_skills: {name}: HTTP {status} {response_body}", file=sys.stderr)
            continue
        published += 1
        print(f"  published: {name}")
    print(f"publish_skills: {published} skill(s) published to {url}")
    return int(failed_publish)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=Path("client/skills"))
    args = parser.parse_args(argv)
    return publish(
        args.source,
        os.environ.get("HYDRA_URL", "http://localhost:8400"),
        os.environ.get("HYDRA_AUTH_TOKEN", ""),
    )


if __name__ == "__main__":
    raise SystemExit(main())
