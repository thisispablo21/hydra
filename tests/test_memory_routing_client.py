from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from hydra_cli import __main__ as cli
from hydra_cli import memory_routing as routing
from hydra_cli import sync

TOPIC = {'slug': 'work', 'title': 'Work', 'description': 'When performing work tasks'}


def memory(mid: int, **kw: Any) -> dict[str, Any]:
    return {'id': mid, 'name': f'm{mid}', 'description': 'desc', 'type': 'user',
            'body': f'SECRET BODY {mid}', 'updated_at': 'now', 'project_slug': None,
            'topics': None, **kw}


@pytest.fixture
def mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    directory = tmp_path / 'mirror'
    snapshot = {'format_version': 1, 'topics': [TOPIC], 'memories': [], 'corpus_nonempty': True}
    monkeypatch.setattr(sync, 'memory_dir_for_cwd', lambda cwd: directory)
    monkeypatch.setattr(sync, 'resolve_project_slug', lambda cwd, **kwargs: 'proj')
    monkeypatch.setattr(sync.api, 'get', lambda path: (200, json.dumps(snapshot)))
    return directory, snapshot


def contents(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes()
            for p in directory.rglob('*') if p.is_file()}


def test_selective_root_complete_catalog_and_bodies(mirror):
    directory, snapshot = mirror
    snapshot['memories'] = [memory(1), memory(2, topics=[]), memory(3, topics=['work']),
                            memory(4, project_slug='proj', type='project', topics=[])]
    assert sync.run_sync('/test') == 0
    root = (directory / 'MEMORY.md').read_text()
    assert '[m1](m1.md)' in root and '[m4](m4.md)' in root
    assert '[m2]' not in root and '[m3]' not in root
    assert root.index('catalog.md') < root.index('Project startup')
    topic = (directory / routing.INDEX_DIR / 'work.md').read_text()
    assert '[m3](../m3.md)' in topic and '[m1]' not in topic
    catalog = (directory / routing.INDEX_DIR / 'catalog.md').read_text()
    for mid in range(1, 5):
        assert f'[m{mid}](../m{mid}.md)' in catalog
        assert f'SECRET BODY {mid}' not in root + topic + catalog
        assert f'SECRET BODY {mid}' in (directory / f'm{mid}.md').read_text()
    parsed = sync.parse_memory_file(directory / 'm4.md')
    assert parsed and parsed['topics'] == [] and parsed['project_slug'] == 'proj'


@pytest.mark.parametrize('failure', ['membership', 'format', 'scope', 'auth', 'network'])
def test_invalid_or_offline_refresh_leaves_all_files(mirror, monkeypatch, failure):
    directory, snapshot = mirror
    snapshot['memories'] = [memory(1)]
    sync.run_sync('/test')
    before = contents(directory)
    if failure == 'membership':
        snapshot['memories'] = [memory(1, topics=['missing'])]
    elif failure == 'format':
        snapshot['format_version'] = 2
    elif failure == 'scope':
        snapshot['memories'] = [memory(1, project_slug='elsewhere')]
    elif failure == 'auth':
        monkeypatch.setattr(sync.api, 'get', lambda path: (401, 'no'))
    else:
        def offline(path):
            raise OSError('offline')
        monkeypatch.setattr(sync.api, 'get', offline)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        sync.run_sync('/test')
    assert contents(directory) == before


@pytest.mark.parametrize('status', [404, 422])
def test_downgrade_keeps_saved_router(mirror, monkeypatch, status):
    directory, snapshot = mirror
    snapshot['memories'] = [memory(1, topics=['work'])]
    sync.run_sync('/test')
    before = contents(directory)
    monkeypatch.setattr(sync.api, 'get', lambda path: (status, 'no'))
    with pytest.raises(RuntimeError, match='last-good'):
        sync.run_sync('/test')
    assert contents(directory) == before


def test_empty_server_preserves_local_catalog_and_reports_bad_membership(mirror):
    directory, snapshot = mirror
    snapshot['memories'] = [memory(1, topics=['work']), memory(2, topics=[])]
    sync.run_sync('/test')
    bad = directory / 'bad.md'
    bad.write_text('---\nid: 99\nname: bad\ntype: user\ntopics: ["unknown"]\n'
                   'project_slug: null\n---\nlocal\n')
    snapshot.update(memories=[], topics=[], corpus_nonempty=False)
    sync.run_sync('/test')
    assert bad.exists() and (directory / 'm1.md').exists() and (directory / 'm2.md').exists()
    root = (directory / 'MEMORY.md').read_text()
    assert 'Recovery index' in root and '[bad](bad.md)' in root and '[m2](m2.md)' in root
    assert (directory / routing.INDEX_DIR / 'work.md').exists()


def test_dry_run_writes_nothing_and_shows_full_review(mirror, capsys):
    directory, snapshot = mirror
    snapshot['memories'] = [memory(1, topics=['work'])]
    sync.run_sync('/test', dry_run=True)
    assert not directory.exists()
    out = capsys.readouterr().out
    assert 'MEMORY.md:' in out and 'work.md:' in out and 'catalog.md:' in out
    assert '[m1](../m1.md)' in out and 'bytes' in out and 'SECRET BODY' not in out


@pytest.mark.parametrize('collision', ['directory', 'body', 'index', 'root'])
def test_unmanaged_and_symlink_collisions_refused(mirror, collision, tmp_path):
    directory, snapshot = mirror
    snapshot['memories'] = [memory(1)]
    directory.mkdir()
    if collision == 'directory':
        (directory / routing.INDEX_DIR).mkdir()
    elif collision == 'body':
        (directory / 'm1.md').write_text('my notes')
    elif collision == 'root':
        elsewhere = tmp_path / 'elsewhere'
        elsewhere.write_text('keep')
        (directory / 'MEMORY.md').symlink_to(elsewhere)
    else:
        sync.run_sync('/test')
        (directory / routing.INDEX_DIR / 'work.md').unlink()
        (directory / routing.INDEX_DIR / 'work.md').symlink_to(directory / 'm1.md')
    before = contents(directory)
    with pytest.raises(ValueError, match='Refusing'):
        sync.run_sync('/test')
    assert contents(directory) == before


def test_root_failure_retains_old_indexes_and_bodies_until_success(mirror, monkeypatch):
    directory, snapshot = mirror
    snapshot['memories'] = [memory(1, topics=['work'])]
    sync.run_sync('/test')
    before_root = (directory / 'MEMORY.md').read_bytes()
    snapshot.update(topics=[], memories=[memory(2)])
    original = routing.atomic_write
    def failing(path, text):
        if path.name == 'MEMORY.md':
            raise OSError('root publication interrupted')
        original(path, text)
    monkeypatch.setattr(routing, 'atomic_write', failing)
    with pytest.raises(OSError):
        sync.run_sync('/test')
    assert (directory / 'MEMORY.md').read_bytes() == before_root
    assert (directory / routing.INDEX_DIR / 'work.md').exists()
    assert (directory / 'm1.md').exists()
    monkeypatch.setattr(routing, 'atomic_write', original)
    sync.run_sync('/test')
    assert (directory / routing.INDEX_DIR / 'work.md').exists()
    assert (directory / 'm1.md').exists()
    sync.run_sync('/test')
    assert not (directory / routing.INDEX_DIR / 'work.md').exists()
    assert not (directory / 'm1.md').exists()


def test_cli_routing_probe_rejects_old_server_before_write(monkeypatch):
    args = cli.build_parser().parse_args(['memory', 'update', '1', '--catalog-only'])
    monkeypatch.setattr(cli.api, 'get', lambda path: (404, 'old'))
    calls = []
    monkeypatch.setattr(cli.api, 'put_json', lambda *a, **kw: calls.append(a))
    with pytest.raises(SystemExit):
        cli.cmd_memory_update(args)
    assert calls == []


def test_cli_refuses_false_routing_success(monkeypatch):
    snapshot = {'format_version': 1, 'topics': [TOPIC], 'memories': [], 'corpus_nonempty': True}
    monkeypatch.setattr(cli.api, 'get', lambda path: (200, json.dumps(snapshot)))
    monkeypatch.setattr(cli.api, 'put_json', lambda *a, **kw: (200, '{"id":1}'))
    monkeypatch.setattr(cli, '_read_body', lambda args: '')
    args = cli.build_parser().parse_args(['memory', 'update', '1', '--catalog-only'])
    with pytest.raises(SystemExit):
        cli.cmd_memory_update(args)


def test_history_cli_query_preserves_exact_body(monkeypatch, capsys):
    args = cli.build_parser().parse_args(['config', 'get-claude-md', '--at',
                                       '2026-10-04T10:00:00+02:00', '--harness', 'codex-cli'])
    paths = []
    monkeypatch.setattr(cli.api, 'get', lambda path: (paths.append(path) or 200, 'exact\n\n'))
    cli.cmd_config_get_claude_md(args)
    assert capsys.readouterr().out == 'exact\n\n'
    assert 'at=2026-10-04T10%3A00%3A00%2B02%3A00' in paths[0]
    assert 'harness=codex-cli' in paths[0]


def test_cli_topic_catalog_put_verifies_readback(tmp_path, monkeypatch, capsys):
    file = tmp_path / 'catalog.json'
    file.write_text(json.dumps([TOPIC]))
    snapshot = {'format_version': 1, 'topics': [], 'memories': [], 'corpus_nonempty': False}
    calls = []
    def get(path):
        return (200, json.dumps(snapshot if path.endswith('snapshot') else [TOPIC]))
    def put(path, payload, *, headers):
        calls.append((path, payload, headers))
        return 200, '{"status":"ok"}'
    monkeypatch.setattr(cli.api, 'get', get)
    monkeypatch.setattr(cli.api, 'put_json', put)
    args = cli.build_parser().parse_args(['memory', 'topics', 'put', str(file), '--flow', 'sync'])
    cli.cmd_memory_topics(args)
    assert calls == [('/api/config/memory-topics', [TOPIC], {'X-Hydra-Flow': 'sync'})]
    assert json.loads(capsys.readouterr().out) == [TOPIC]


def test_cli_update_omits_routing_and_sends_explicit_null(monkeypatch):
    snapshot = {'format_version': 1, 'topics': [], 'memories': [], 'corpus_nonempty': False}
    monkeypatch.setattr(cli.api, 'get', lambda path: (200, json.dumps(snapshot)))
    monkeypatch.setattr(cli, '_read_body', lambda args: '')
    writes = []
    def put(path, payload, **kw):
        writes.append(payload)
        return 200, json.dumps(payload)
    monkeypatch.setattr(cli.api, 'put_json', put)
    parser = cli.build_parser()
    cli.cmd_memory_update(parser.parse_args(['memory', 'update', '1', '--desc', 'new']))
    cli.cmd_memory_update(parser.parse_args(['memory', 'update', '1', '--unclassified']))
    assert 'topics' not in writes[0]
    assert writes[1]['topics'] is None


def test_cli_routing_filters_preserve_existing_scope(monkeypatch, capsys):
    rows = [memory(1), memory(2, topics=[]), memory(3, topics=['work'])]
    monkeypatch.setattr(cli, 'fetch_server_memories', lambda slug: rows)
    args = cli.build_parser().parse_args(['memory', 'list', '--project', 'proj', '--topic', 'work'])
    cli.cmd_memory_list(args)
    out = capsys.readouterr()
    assert 'm3' in out.out and 'm1' not in out.out and 'm2' not in out.out
    assert 'proj + global' in out.err


def test_config_publication_passes_session_header(tmp_path, monkeypatch):
    file = tmp_path / 'instructions.md'
    file.write_text('exact\n\n')
    monkeypatch.delenv('CLAUDE_CODE_SESSION_ID', raising=False)
    monkeypatch.setenv('CODEX_SESSION_ID', 'session-test')
    calls = []
    def put(path, text, *, headers):
        calls.append((path, text, headers))
        return 200, '{}'
    monkeypatch.setattr(cli.api, 'put_text', put)
    args = cli.build_parser().parse_args(['config', 'put-claude-md', str(file)])
    cli.cmd_config_put_claude_md(args)
    assert calls == [('/api/config/claude-md', 'exact\n\n', {'X-Session-Id': 'session-test'})]


@pytest.mark.parametrize('stage', ['body', 'topic', 'catalog'])
def test_interrupted_refresh_keeps_existing_root_links(mirror, monkeypatch, stage):
    directory, snapshot = mirror
    snapshot['memories'] = [memory(1, topics=['work'])]
    sync.run_sync('/test')
    old_root = (directory / 'MEMORY.md').read_bytes()
    snapshot['memories'] = [memory(2, topics=['work'])]
    original = routing.atomic_write
    names = {'body': 'm2.md', 'topic': 'work.md', 'catalog': 'catalog.md'}
    def failure(path, text):
        if path.name == names[stage]:
            raise OSError('injected interruption')
        original(path, text)
    monkeypatch.setattr(routing, 'atomic_write', failure)
    with pytest.raises(OSError):
        sync.run_sync('/test')
    assert (directory / 'MEMORY.md').read_bytes() == old_root
    assert (directory / 'm1.md').exists()
    # Every body link remaining in a published topic or catalog still resolves.
    import re
    for path in (directory / routing.INDEX_DIR).glob('*.md'):
        for body in re.findall(r'\]\(\.\./([^)]*)\)', path.read_text()):
            assert (directory / body).exists()


def test_atomic_replace_failure_preserves_previous_file(tmp_path, monkeypatch):
    target = tmp_path / 'MEMORY.md'
    target.write_text('previous\n')
    def failure(self, destination):
        raise OSError('replace refused')
    monkeypatch.setattr(Path, 'replace', failure)
    with pytest.raises(OSError):
        routing.atomic_write(target, 'next\n')
    assert target.read_text() == 'previous\n'
    assert list(tmp_path.iterdir()) == [target]


def test_prunes_only_owned_indexes_after_grace_period(mirror):
    directory, snapshot = mirror
    snapshot['memories'] = [memory(1, topics=['work'])]
    sync.run_sync('/test')
    note = directory / routing.INDEX_DIR / 'user-note.txt'
    note.write_text('keep')
    snapshot.update(topics=[], memories=[memory(2)])
    sync.run_sync('/test')
    sync.run_sync('/test')
    assert note.read_text() == 'keep'
    assert not (directory / routing.INDEX_DIR / 'work.md').exists()


def test_empty_snapshot_discovers_unparseable_local_file(mirror):
    directory, snapshot = mirror
    directory.mkdir()
    note = directory / 'notes.md'
    note.write_text('# Raw local note')
    snapshot.update(memories=[], corpus_nonempty=False)
    sync.run_sync('/test')
    assert note.read_text() == '# Raw local note'
    assert '[notes](notes.md)' in (directory / 'MEMORY.md').read_text()
    assert '[notes](../notes.md)' in (directory / routing.INDEX_DIR / 'catalog.md').read_text()


def test_preview_reports_startup_budgets(capsys):
    routing.preview('x' * 26000, {}, [])
    out = capsys.readouterr().err
    assert '25000-byte' in out and 'Codex' not in out
    routing.preview('x' * 32000, {}, [])
    assert 'Codex' in capsys.readouterr().err


def test_frontmatter_roundtrips_multiline_metadata(tmp_path):
    mem = memory(1, name='quoted "name"\nnext', description='first\n---\nlast')
    target = tmp_path / 'm.md'
    target.write_text(sync.serialize_memory(mem))
    parsed = sync.parse_memory_file(target)
    assert parsed and parsed['name'] == mem['name'] and parsed['description'] == mem['description']
    assert parsed['topics'] is None and parsed['project_slug'] is None


def test_pinned_frontmatter_roundtrip_preserves_json_scope_and_topics(tmp_path):
    target = tmp_path / 'pinned.md'
    original = memory(1, project_slug='unicode-é-project', topics=['work', 'other'])
    target.write_text(sync.serialize_memory(original))
    parsed = sync.parse_memory_file(target)
    assert parsed and parsed['project_slug'] == original['project_slug']
    assert parsed['topics'] == original['topics']
    assert parsed['routing_scope_known'] is True and parsed['routing_invalid'] is False


def test_legacy_scope_unknown_remains_unclassified_in_recovery(tmp_path):
    target = tmp_path / 'legacy.md'
    target.write_text('---\nname: legacy\ntype: project\ntopics: ["work"]\n---\nbody')
    parsed = sync.parse_memory_file(target)
    assert parsed and parsed['routing_scope_known'] is False
    root, _, _ = routing.render_indexes([(target, parsed)], [TOPIC], 'proj', recovery=True)
    assert '[legacy](legacy.md)' in root.split('## Unclassified', 1)[1]


def test_recovery_index_links_escape_local_filenames(mirror):
    directory, snapshot = mirror
    directory.mkdir()
    note = directory / 'note with (parentheses).md'
    note.write_text('# Preserve this local note')
    snapshot.update(memories=[], corpus_nonempty=False)
    sync.run_sync('/test')
    root = (directory / 'MEMORY.md').read_text()
    assert '(note%20with%20%28parentheses%29.md)' in root
    assert note.read_text() == '# Preserve this local note'


async def test_actual_old_fastapi_int_route_422_uses_flat_fallback(tmp_path, monkeypatch):
    old = FastAPI()
    row = memory(1)
    @old.get('/api/projects')
    async def projects():
        return [{'slug': 'proj', 'paths': [{'path': '/test/proj'}]}]
    @old.get('/api/memory')
    async def memories():
        return [row]
    @old.get('/api/memory/{memory_id}')
    async def get_memory(memory_id: int):
        return row
    directory = tmp_path / 'mirror'
    monkeypatch.setattr(sync, 'memory_dir_for_cwd', lambda cwd: directory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=old), base_url='http://old',
    ) as client:
        response = await client.get('/api/memory/snapshot?project_slug=proj')
        assert response.status_code == 422
        assert response.json()['detail'][0]['loc'] == ['path', 'memory_id']
        paths = ['/api/projects', '/api/memory?project_slug=proj&include_global=true',
                 '/api/memory/snapshot?project_slug=proj']
        responses = {}
        for path in paths:
            response = await client.get(path)
            responses[path] = (response.status_code, response.text)
    monkeypatch.setattr(sync.api, 'get', lambda path: responses[path])
    assert sync.run_sync('/test/proj') == 0
    assert (directory / 'MEMORY.md').read_text() == '- [m1](m1.md) - desc\n'
    assert not (directory / routing.INDEX_DIR).exists()


@pytest.mark.parametrize(('root', 'warns'), [
    ('x\n' * 200, False), ('x\n' * 201, True),
    ('é' * 12500, False), ('é' * 12501, True),
])
def test_shared_budget_boundaries_count_lines_and_utf8_bytes(capsys, root, warns):
    routing.warn_root_budget(root)
    captured = capsys.readouterr()
    assert captured.out == ''
    assert ('200-line' in captured.err) is warns


def test_routed_actual_write_warns_but_publishes_complete_root(mirror, capsys):
    directory, snapshot = mirror
    description = 'é' * 12501
    snapshot['memories'] = [memory(1, description=description)]
    assert sync.run_sync('/test') == 0
    captured = capsys.readouterr()
    assert '25000-byte budget' in captured.err
    assert 'WARNING' not in captured.out
    assert description in (directory / 'MEMORY.md').read_text()
    assert len((directory / 'MEMORY.md').read_bytes()) > 25000


def test_legacy_actual_write_warns_but_publishes_complete_root(tmp_path, monkeypatch, capsys):
    directory = tmp_path / 'legacy-mirror'
    description = 'é' * 12501
    row = memory(1, description=description)
    monkeypatch.setattr(sync, 'memory_dir_for_cwd', lambda cwd: directory)
    monkeypatch.setattr(sync, 'resolve_project_slug', lambda cwd, **kw: 'proj')
    def get(path):
        return (422, '{}') if path.startswith('/api/memory/snapshot') else (200, json.dumps([row]))
    monkeypatch.setattr(sync.api, 'get', get)
    assert sync.run_sync('/test') == 0
    captured = capsys.readouterr()
    assert '25000-byte budget' in captured.err and 'WARNING' not in captured.out
    assert description in (directory / 'MEMORY.md').read_text()


def test_sync_allows_symlinked_ancestor_without_relaxing_owned_file_checks(mirror, tmp_path):
    directory, snapshot = mirror
    physical = tmp_path / 'physical'
    physical.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(physical, target_is_directory=True)
    through_alias = alias / 'mirror'
    snapshot['memories'] = [memory(1)]
    sync._run_snapshot_sync('proj', through_alias, snapshot, dry_run=False)
    assert (physical / 'mirror' / 'MEMORY.md').is_file()
    body = through_alias / 'm1.md'
    body.unlink()
    outside = tmp_path / 'outside.md'
    outside.write_text('keep outside')
    body.symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        sync._run_snapshot_sync('proj', through_alias, snapshot, dry_run=False)
    assert outside.read_text() == 'keep outside'


@pytest.mark.parametrize('kind', ['root', 'index-dir', 'index', 'manifest'])
@pytest.mark.parametrize('location', ['ancestor', 'mirror'])
def test_owned_symlinks_remain_refused_with_symlinked_ancestor(tmp_path, kind, location):
    physical = tmp_path / 'physical'
    physical.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(physical, target_is_directory=True)
    directory = alias / 'mirror'
    directory.mkdir()
    if location == 'mirror':
        mirror_alias = tmp_path / 'mirror-alias'
        mirror_alias.symlink_to(directory.resolve(), target_is_directory=True)
        directory = mirror_alias
    outside = tmp_path / 'outside'
    outside.mkdir()
    if kind == 'root':
        (directory / 'MEMORY.md').symlink_to(outside / 'root.md')
    elif kind == 'index-dir':
        (directory / routing.INDEX_DIR).symlink_to(outside, target_is_directory=True)
    else:
        indexes = directory / routing.INDEX_DIR
        indexes.mkdir()
        if kind == 'manifest':
            (indexes / routing.MANIFEST).symlink_to(outside / 'state.json')
        else:
            (indexes / routing.MANIFEST).write_text(json.dumps({
                'format_version': 1, 'files': ['catalog.md'], 'topics': [],
            }))
            (indexes / 'catalog.md').symlink_to(outside / 'catalog.md')
    with pytest.raises(ValueError, match='symlink|unmanaged'):
        routing.check_paths(directory, [], ['catalog.md'])


def test_restored_cwd_encoder_keeps_distinct_paths_and_ignores_overrides(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    root = tmp_path / 'checkout'
    child = root / 'child'
    child.mkdir(parents=True)
    (root / '.git').mkdir()
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setenv('HYDRA_MEMORY_DIR', str(tmp_path / 'inherited'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'custom-config'))
    monkeypatch.setenv('CLAUDE_COWORK_MEMORY_PATH_OVERRIDE', str(tmp_path / 'native-override'))
    root_directory = sync.memory_dir_for_cwd(str(root))
    child_directory = sync.memory_dir_for_cwd(str(child))
    assert root_directory != child_directory
    assert root_directory.parent.parent == home / '.claude/projects'
    assert child_directory.parent.parent == home / '.claude/projects'
    assert root_directory.parent.name.endswith('checkout')
    assert child_directory.parent.name.endswith('checkout-child')


def test_sync_allows_direct_memory_directory_symlink(mirror, tmp_path):
    _, snapshot = mirror
    target = tmp_path / 'memory-target'
    target.mkdir()
    directory = tmp_path / 'memory-link'
    directory.symlink_to(target, target_is_directory=True)
    snapshot['memories'] = [memory(1)]
    assert sync._run_snapshot_sync('proj', directory, snapshot, dry_run=False) == 0
    assert (target / 'MEMORY.md').is_file() and (target / 'm1.md').is_file()
    assert (target / routing.INDEX_DIR / 'catalog.md').is_file()
