"""`hydra project update` has no --path: paths are per machine and move with
`project attach`. The flag used to be accepted and silently dropped (#262)."""

from __future__ import annotations

import json

import pytest
from hydra_cli import __main__ as main_mod


def test_update_rejects_path_flag():
    parser = main_mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["project", "update", "hydra", "--path", "/new/path"])


def test_update_sends_description_only(monkeypatch, capsys):
    sent: list[tuple[str, dict]] = []

    def put_json(path: str, payload: dict) -> tuple[int, str]:
        sent.append((path, payload))
        return 200, json.dumps({"slug": "hydra", "description": payload["description"]})

    monkeypatch.setattr(main_mod.api, "put_json", put_json)
    args = main_mod.build_parser().parse_args(
        ["project", "update", "hydra", "--desc", "control plane"]
    )
    main_mod.cmd_project_update(args)
    assert sent == [("/api/projects/hydra", {"description": "control plane"})]


def test_update_without_desc_exits():
    args = main_mod.build_parser().parse_args(["project", "update", "hydra"])
    with pytest.raises(SystemExit):
        main_mod.cmd_project_update(args)
