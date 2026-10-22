"""Validate distribution snapshots and publish selective, owned memory indexes."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

INDEX_DIR = '.hydra-indexes'
MANIFEST = 'manifest.json'
SLUG = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
VALID_TYPES = {'user', 'feedback', 'project', 'reference'}


def validate_topics(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError('Topic catalog must be an array')
    seen: set[str] = set()
    for topic in value:
        if not isinstance(topic, dict) or any(
            not isinstance(topic.get(key), str) for key in ('slug', 'title', 'description')
        ):
            raise ValueError('Invalid topic catalog entry')
        slug = topic['slug']
        if not SLUG.fullmatch(slug) or slug == 'catalog' or slug in seen:
            raise ValueError(f'Unsafe, reserved or duplicate topic slug: {slug}')
        if not topic['title'].strip() or not topic['description'].strip():
            raise ValueError(f'Topic {slug} needs a title and activity description')
        seen.add(slug)
    return value


def validate_snapshot(value: Any, project_slug: str | None) -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get('format_version')) is not int:
        raise ValueError('Invalid memory snapshot')
    if value['format_version'] != 1 or type(value.get('corpus_nonempty')) is not bool:
        raise ValueError('Unsupported memory snapshot format')
    topics = validate_topics(value.get('topics'))
    known = {topic['slug'] for topic in topics}
    rows = value.get('memories')
    if not isinstance(rows, list):
        raise ValueError('Snapshot memories must be an array')
    ids: set[int] = set()
    names: set[str] = set()
    for mem in rows:
        if not isinstance(mem, dict):
            raise ValueError('Invalid memory row')
        if type(mem.get('id')) is not int or mem['id'] <= 0 or mem['id'] in ids:
            raise ValueError('Invalid or duplicate memory id')
        if any(not isinstance(mem.get(key), str) for key in (
            'name', 'description', 'type', 'body', 'updated_at'
        )) or not mem['name'] or mem['name'] in names:
            raise ValueError('Invalid or duplicate memory fields')
        if mem['type'] not in VALID_TYPES:
            raise ValueError('Invalid memory type')
        if 'project_slug' not in mem or mem['project_slug'] not in (None, project_slug):
            raise ValueError('Snapshot contains an out-of-scope memory')
        if 'topics' not in mem:
            raise ValueError('Snapshot lacks routing metadata')
        memberships = mem['topics']
        if memberships is not None and (
            not isinstance(memberships, list)
            or any(not isinstance(slug, str) or slug not in known for slug in memberships)
            or len(set(memberships)) != len(memberships)
        ):
            raise ValueError(f'Invalid topic membership for memory {mem["id"]}')
        ids.add(mem['id'])
        names.add(mem['name'])
    if rows and not value['corpus_nonempty']:
        raise ValueError('Snapshot corpus flag contradicts its memories')
    return value


def atomic_write(path: Path, text: str) -> None:
    if path.is_symlink():
        raise ValueError(f'Refusing symlink: {path}')
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    tmp = Path(temporary)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(text)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def load_manifest(memory_dir: Path) -> dict[str, Any] | None:
    directory = memory_dir / INDEX_DIR
    if directory.is_symlink():
        raise ValueError(f'Refusing symlink: {directory}')
    path = directory / MANIFEST
    if path.is_symlink():
        raise ValueError(f'Refusing symlink: {path}')
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('format_version') != 1:
        raise ValueError('Invalid owned-index manifest')
    files = value.get('files')
    if not isinstance(files, list) or any(
        not isinstance(name, str)
        or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}\.md', name)
        for name in files
    ):
        raise ValueError('Invalid owned-index filenames')
    validate_topics(value.get('topics'))
    active = value.get('active_files', files)
    if not isinstance(active, list) or any(name not in files for name in active):
        raise ValueError('Invalid active index filenames')
    links = value.get('index_bodies', {})
    if not isinstance(links, dict) or any(
        name not in files or not isinstance(bodies, list)
        or any(not isinstance(body, str) or Path(body).name != body or not body.endswith('.md')
               for body in bodies)
        for name, bodies in links.items()
    ):
        raise ValueError('Invalid retained body filenames')
    return value


def check_paths(memory_dir: Path, body_names: list[str], index_names: list[str]) -> None:
    manifest = load_manifest(memory_dir)
    directory = memory_dir / INDEX_DIR
    if directory.exists() and (not directory.is_dir() or manifest is None):
        raise ValueError(f'Refusing unmanaged index directory: {directory}')
    owned = set(manifest['files']) if manifest else set()
    for name in set(index_names) | owned:
        path = directory / name
        if path.is_symlink() or (path.exists() and name not in owned):
            raise ValueError(f'Refusing unmanaged index path: {path}')
    local_names = [path.name for path in memory_dir.glob('*.md')]
    for name in [*body_names, *local_names, 'MEMORY.md']:
        path = memory_dir / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f'Refusing unsafe mirror path: {path}')


def _label(text: str) -> str:
    return text.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]').replace(
        '\n', ' '
    ).replace('\r', ' ')


def _entry(name: str, mem: dict[str, Any], prefix: str = '') -> str:
    desc = _label(mem.get('description', '').strip())
    suffix = f' - {desc}' if desc else ''
    return f'- [{_label(mem["name"])}]({prefix}{quote(name)}){suffix}'


def render_indexes(
    entries: list[tuple[Path, dict[str, Any]]], topics: list[dict[str, str]],
    project_slug: str | None, *, recovery: bool = False,
) -> tuple[str, dict[str, str], list[str]]:
    ordered = sorted(entries, key=lambda item: item[1]['name'])
    known = {topic['slug'] for topic in topics}
    root = [
        '# Memory router',
        'Read a topic index when its activity matches the task, then open only relevant bodies.',
        'Reconsider retrieval as the task expands; topic labels do not limit applicability.',
        'If routing is unclear or an index is missing, use the catalog or local memory files.',
        'Reuse unchanged bodies already in context; after compaction retrieve missing rules again.',
        f'[Full memory catalog]({INDEX_DIR}/catalog.md) - all scoped memories.',
        '',
    ]
    indexes: dict[str, str] = {}
    diagnostics: list[str] = []
    for topic in topics:
        root.append(
            f'- [{_label(topic["title"])}]({INDEX_DIR}/{topic["slug"]}.md)'
            f' - {_label(topic["description"])}'
        )
        lines = [f'# {_label(topic["title"])}', _label(topic['description']), '']
        lines.extend(_entry(path.name, mem, '../') for path, mem in ordered
                     if isinstance(mem.get('topics'), list) and topic['slug'] in mem['topics'])
        indexes[f'{topic["slug"]}.md'] = '\n'.join(lines) + '\n'
    root.extend(['', '## Project startup index'])
    root.extend(_entry(path.name, mem) for path, mem in ordered
                if project_slug is not None and mem.get('project_slug') == project_slug)
    root.extend(['', '## Unclassified'])
    for path, mem in ordered:
        memberships = mem.get('topics')
        invalid = mem.get('routing_invalid', False) or (
            memberships is not None and (
                not isinstance(memberships, list)
                or any(not isinstance(slug, str) or slug not in known for slug in memberships)
            )
        )
        if invalid:
            diagnostics.append(f'{path.name}: invalid membership; shown as unclassified')
        if (memberships is None or invalid or not mem.get('routing_scope_known', True)) and (
            project_slug is None or mem.get('project_slug') != project_slug
        ):
            root.append(_entry(path.name, mem))
            diagnostics.append(f'{path.name}: unclassified')
    if recovery:
        root.extend(['', '## Recovery index - server corpus is empty'])
        root.extend(_entry(path.name, mem) for path, mem in ordered)
        diagnostics.append('Empty server: preserved local bodies; pruning disabled')
    catalog = ['# Full memory catalog', 'Open individual bodies as needed.', '']
    catalog.extend(_entry(path.name, mem, '../') for path, mem in ordered)
    indexes['catalog.md'] = '\n'.join(catalog) + '\n'
    return '\n'.join(root) + '\n', indexes, diagnostics


def publish_indexes(
    memory_dir: Path, root: str, indexes: dict[str, str], topics: list[dict[str, str]],
) -> set[str]:
    previous = load_manifest(memory_dir)
    old_files = set(previous['files']) if previous else set()
    old_active = set(previous.get('active_files', previous['files'])) if previous else set()
    old_links = previous.get('index_bodies', {}) if previous else {}
    directory = memory_dir / INDEX_DIR
    directory.mkdir(exist_ok=True)
    # Claim the union before replacement so interrupted publications remain owned.
    union = {'format_version': 1, 'files': sorted(old_files | indexes.keys()), 'topics': topics,
             'active_files': sorted(old_active), 'index_bodies': old_links}
    atomic_write(directory / MANIFEST, json.dumps(union, indent=2) + '\n')
    for name, text in indexes.items():
        atomic_write(directory / name, text)
    warn_root_budget(root)
    atomic_write(memory_dir / 'MEMORY.md', root)
    obsolete = old_files - old_active - indexes.keys()
    for name in sorted(obsolete):
        path = directory / name
        if path.is_symlink():
            raise ValueError(f'Refusing obsolete index symlink: {path}')
        path.unlink(missing_ok=True)
    retained = old_active - indexes.keys()
    links = {name: old_links.get(name, []) for name in retained}
    links.update({name: [unquote(body) for body in re.findall(r'\]\(\.\./([^)]*)\)', text)]
                  for name, text in indexes.items()})
    union['files'] = sorted(retained | indexes.keys())
    union['active_files'] = sorted(indexes)
    union['index_bodies'] = links
    atomic_write(directory / MANIFEST, json.dumps(union, indent=2) + '\n')
    return {body for name in retained for body in links[name]}


def warn_root_budget(root: str) -> None:
    lines, size = len(root.splitlines()), len(root.encode('utf-8'))
    if lines > 200 or size > 25000:
        print('WARNING: root exceeds Claude 200-line or conservative 25000-byte budget',
              file=sys.stderr)
    if size > 31500:
        print('WARNING: root may exceed Codex 32000-byte budget including header/trailer',
              file=sys.stderr)


def preview(root: str, indexes: dict[str, str], diagnostics: list[str]) -> None:
    for name, text in [('MEMORY.md', root), *indexes.items()]:
        lines, size = len(text.splitlines()), len(text.encode('utf-8'))
        print(f'\n--- {name}: {lines} lines, {size} bytes ---\n{text}', end='')
        if name == 'MEMORY.md':
            warn_root_budget(text)
    for diagnostic in diagnostics:
        print(f'Diagnostic: {diagnostic}')
