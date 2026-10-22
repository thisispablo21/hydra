from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "publish_skills.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("publish_skills", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish_skills = _load_script()


def _skill(root: Path, name: str = "review", common: str | None = None) -> Path:
    path = root / name
    path.mkdir()
    path.joinpath("common.md").write_text(
        common or f"---\nname: {name}\ndescription: Review changes\n---\n\nDo it.\n"
    )
    return path


def test_build_body_from_source_files(tmp_path: Path):
    path = _skill(tmp_path)
    path.joinpath("codex-cli.json").write_text('{"tool": "codex"}')
    path.joinpath("skill.json").write_text(
        '{"enabled": false, "implicit_invocation": true, "instances": ["work"]}'
    )

    assert publish_skills.build_skill(path) == {
        "kind": "skill",
        "enabled": False,
        "implicit_invocation": True,
        "instances": ["work"],
        "common": path.joinpath("common.md").read_text(),
        "variants": {"codex-cli": {"tool": "codex"}},
    }


def test_build_body_defaults(tmp_path: Path):
    path = _skill(tmp_path)
    body = publish_skills.build_skill(path)
    assert body["enabled"] is True
    assert body["implicit_invocation"] is False
    assert body["instances"] is None
    assert body["variants"] == {}


def test_instructions_need_no_frontmatter(tmp_path: Path):
    path = _skill(tmp_path, "instructions", "Plain instructions.\n")
    assert publish_skills.build_skill(path)["kind"] == "instructions"


@pytest.mark.parametrize(
    "setup",
    [
        lambda path: path.joinpath("common.md").unlink(),
        lambda path: path.joinpath("common.md").write_text("No frontmatter.\n"),
        lambda path: path.joinpath("common.md").write_text(
            "---\nname: wrong\ndescription: Fine\n---\n"
        ),
        lambda path: path.joinpath("common.md").write_text(
            "---\nname: review\ndescription:\n---\n"
        ),
        lambda path: path.joinpath("skill.json").write_text('{"surprise": true}'),
        lambda path: path.joinpath("claude-code.json").write_text("{"),
        lambda path: path.joinpath("claude-code.json").write_text('{"slot": 1}'),
    ],
    ids=[
        "missing-common",
        "no-frontmatter",
        "wrong-name",
        "empty-description",
        "unknown-metadata",
        "malformed-variant",
        "non-string-variant",
    ],
)
def test_validation_stops_all_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: Any,
):
    good = _skill(tmp_path, "good")
    bad = _skill(tmp_path)
    setup(bad)
    calls: list[str] = []
    monkeypatch.setattr(
        publish_skills,
        "put_skill",
        lambda url, token, name, body: (calls.append(name) or 200, ""),
    )

    assert publish_skills.publish(tmp_path, "http://hydra", "token") == 1
    assert good.is_dir()
    assert calls == []


def test_shipped_debug_hydra_body():
    path = ROOT / "client" / "skills" / "debug-hydra"
    common = path.joinpath("common.md").read_text()
    assert publish_skills.build_skill(path) == {
        "kind": "skill",
        "enabled": True,
        "implicit_invocation": False,
        "instances": None,
        "common": common,
        "variants": {},
    }


def test_http_failure_does_not_stop_other_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _skill(tmp_path, "bad")
    _skill(tmp_path, "good")
    calls: list[str] = []

    def put_skill(url: str, token: str, name: str, body: dict[str, Any]) -> tuple[int, str]:
        calls.append(name)
        return (422, "invalid") if name == "bad" else (200, "ok")

    monkeypatch.setattr(publish_skills, "put_skill", put_skill)
    assert publish_skills.publish(tmp_path, "http://hydra", "token") == 1
    assert calls == ["bad", "good"]


def test_publication_carries_instance_and_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('HYDRA_INSTANCE_ID', 'instance-test')
    monkeypatch.delenv('CLAUDE_CODE_SESSION_ID', raising=False)
    monkeypatch.setenv('CODEX_SESSION_ID', 'session-test')
    requests = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b'{}'
    def urlopen(request):
        requests.append(request)
        return Response()
    monkeypatch.setattr(publish_skills.urllib.request, 'urlopen', urlopen)
    result = publish_skills.put_skill('http://isolated', 'test-token', 'instructions', {})
    assert result == (200, '{}')
    assert requests[0].get_header('X-instance-id') == 'instance-test'
    assert requests[0].get_header('X-session-id') == 'session-test'
