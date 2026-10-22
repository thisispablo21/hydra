"""`hydra memory list` scoping and output format.

The corpus is dominated by bodies, so `list` defaults to this project's index.
These tests pin the two things that regress silently: which rows come back for
each scope flag, and that resolving the default scope never writes (the project
lookup must not auto-register).
"""

from __future__ import annotations

import json

import pytest
from hydra_cli import __main__ as main_mod
from hydra_cli import sync as sync_mod

from tests.test_sync import FakeAPI

PROJECTS = [{"slug": "ars", "path": "/test/proj"}]

MEMORIES = [
    {"id": 1, "name": "global-one", "type": "user", "description": "a user fact",
     "body": "x" * 500, "project_slug": None, "topics": ["infrastructure", "git"]},
    {"id": 2, "name": "ars-one", "type": "project", "description": "an ars fact",
     "body": "y" * 500, "project_slug": "ars", "topics": None},
    {"id": 3, "name": "other-one", "type": "project", "description": "elsewhere",
     "body": "z" * 500, "project_slug": "pquant"},
    {"id": 4, "name": "global-two", "type": "feedback", "description": None,
     "body": "w" * 500, "project_slug": None, "topics": []},
]


class RecordingAPI(FakeAPI):
    """FakeAPI that logs POSTs, so a test can assert nothing was written."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.posts: list[str] = []

    def post(self, path: str, payload: dict) -> tuple[int, str]:
        self.posts.append(path)
        return super().post(path, payload)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> RecordingAPI:
    fake = RecordingAPI(
        projects=[dict(p) for p in PROJECTS],
        memories=[dict(m) for m in MEMORIES],
    )
    # sync_mod.api is the hydra_cli.api module object, shared with __main__ -
    # patching it here covers both call sites.
    monkeypatch.setattr(sync_mod.api, "get", fake.get)
    monkeypatch.setattr(sync_mod.api, "post", fake.post)
    return fake


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Pin cwd to the registered project path and record how it was called."""
    seen: dict = {}

    def spy(cwd: str, *, auto_attach: bool = True):
        seen["cwd"] = cwd
        seen["auto_attach"] = auto_attach
        return sync_mod.resolve_project_slug("/test/proj", auto_attach=auto_attach)

    monkeypatch.setattr(main_mod, "resolve_project_slug", spy)
    return seen


def run(*argv: str) -> None:
    args = main_mod.build_parser().parse_args(["memory", "list", *argv])
    main_mod.cmd_memory_list(args)


def ids(out: str) -> list[int]:
    return [int(line.split()[0]) for line in out.strip().splitlines() if line.strip()]


def test_default_scopes_to_this_project_plus_globals(api, resolver, capsys):
    run()
    out = capsys.readouterr()
    assert ids(out.out) == [1, 2, 4]
    assert "3 memories (ars + global)" in out.err


def test_default_resolution_never_auto_registers(api, resolver, capsys):
    """A read must not create or attach a project row as a side effect."""
    run()
    capsys.readouterr()
    assert resolver["auto_attach"] is False
    assert "/api/projects/auto-register" not in api.posts
    assert api.posts == []


def test_all_returns_every_scope(api, capsys):
    run("--all")
    assert ids(capsys.readouterr().out) == [1, 2, 3, 4]


def test_project_flag_selects_another_project(api, capsys):
    run("--project", "pquant")
    out = capsys.readouterr()
    assert ids(out.out) == [1, 3, 4]
    assert "pquant + global" in out.err


def test_global_flag_returns_only_globals(api, capsys):
    run("--global")
    out = capsys.readouterr()
    assert ids(out.out) == [1, 4]
    assert "2 memories (global)" in out.err


def test_unregistered_cwd_falls_back_to_globals(
    api, capsys, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        main_mod, "resolve_project_slug", lambda cwd, **kw: None
    )
    run()
    out = capsys.readouterr()
    assert ids(out.out) == [1, 4]
    assert "No project registered" in out.err


def test_contained_cwd_scopes_to_confirmed_parent(
    api, capsys, monkeypatch: pytest.MonkeyPatch
):
    api.projects = [{
        "slug": "ars",
        "auto_registered_at": None,
        "paths": [{"instance_id": "test", "path": "/test/proj"}],
    }]
    monkeypatch.setattr(main_mod.os, "getcwd", lambda: "/test/proj/src")

    run()

    out = capsys.readouterr()
    assert ids(out.out) == [1, 2, 4]
    assert "3 memories (ars + global)" in out.err
    assert api.posts == []


def test_equal_depth_containment_ambiguity_falls_back_to_globals(
    api, capsys, monkeypatch: pytest.MonkeyPatch
):
    api.projects = [
        {
            "slug": slug,
            "auto_registered_at": None,
            "paths": [{"instance_id": "test", "path": "/test/proj"}],
        }
        for slug in ("alpha", "beta")
    ]
    monkeypatch.setattr(main_mod.os, "getcwd", lambda: "/test/proj/src")

    run()

    out = capsys.readouterr()
    assert ids(out.out) == [1, 4]
    assert "No project registered" in out.err
    assert api.posts == []


def test_json_keeps_full_rows(api, resolver, capsys):
    run("--json")
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == [1, 2, 4]
    assert rows[0]["body"] == "x" * 500


def test_brief_line_carries_the_index_and_drops_the_body(api, resolver, capsys):
    run()
    out = capsys.readouterr().out
    assert "2 project ars [unclassified] - ars-one - an ars fact" in out
    assert "1 user GLOBAL [infrastructure,git] - global-one - a user fact" in out
    assert "y" * 500 not in out
    # description=None must not print "None"; [] is catalog-only
    assert "4 feedback GLOBAL [catalog] - global-two - " in out


def test_scope_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        main_mod.build_parser().parse_args(
            ["memory", "list", "--all", "--global"]
        )
