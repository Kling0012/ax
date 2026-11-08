"""Collect AX registrations and retain the last successful snapshot."""
import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml


def collect(root):
    registry = yaml.safe_load((root / 'plugins/registry.yaml').read_text())
    fragments = sorted((root / 'plugins/registry.d').glob('*.yaml'))
    compose = sorted((root / 'compose.d').glob('*.yaml'))
    if not fragments or not compose or not isinstance(registry, dict) or not isinstance(registry.get('plugins'), list):
        raise ValueError('missing_sources')
    plugins = []
    for path in fragments:
        doc = yaml.safe_load(path.read_text())
        if not isinstance(doc, dict) or not isinstance(doc.get('plugins'), list):
            raise ValueError('invalid_fragment')
        plugins.extend(doc['plugins'])
    expected = {(p['id'], p['kind'], p['path']) for p in plugins}
    actual = {(p['id'], p['kind'], p['path']) for p in registry['plugins']}
    if len(expected) != len(plugins) or expected != actual:
        raise ValueError('registry_mismatch')
    services = set()
    for path in compose:
        doc = yaml.safe_load(path.read_text())
        if not isinstance(doc, dict) or not isinstance(doc.get('services', {}), dict):
            raise ValueError('invalid_compose')
        services.update(doc.get('services', {}))
    source = Path(os.environ.get('AX_ENTRY_SITES_FILE', '/run/ax-views/sites.json'))
    data = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('sites'), list):
        raise ValueError('invalid_sites')
    for row in data['sites']:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not isinstance(row.get('name'), str) or not isinstance(row.get('url'), str)
                or row.get('exposure') not in ('public', 'tailnet')):
            raise ValueError('invalid_site')
    return {'sites': data['sites'], 'external': 'excluded'}


def generate(root, destination):
    destination = Path(destination)
    try:
        old = json.loads(destination.read_text())
        if not isinstance(old, dict) or not all(k in old for k in ('data', 'last_success_at', 'latest')):
            raise ValueError('invalid_state')
    except FileNotFoundError:
        old = {'data': None, 'last_success_at': None, 'latest': None}
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        data = collect(Path(root))
        result = {'data': data, 'last_success_at': stamp, 'latest': {'status': 'success', 'at': stamp}}
    except Exception:
        result = {'data': old['data'], 'last_success_at': old['last_success_at'],
                  'latest': {'status': 'failed', 'at': stamp, 'error': 'collection_failed'}}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent, delete=False) as stream:
        os.fchmod(stream.fileno(), 0o644)
        json.dump(result, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
        tmp = stream.name
    os.replace(tmp, destination)
    if result['latest']['status'] == 'failed':
        raise RuntimeError('collection_failed')
    return result


async def run(ctx):
    await asyncio.to_thread(generate, os.environ.get('AX_HOME_IN_CONTAINER', '/ax'),
                            os.environ.get('AX_ENTRY_STATE_FILE', '/entry-index/state.json'))
    return {'result': 'stored'}
