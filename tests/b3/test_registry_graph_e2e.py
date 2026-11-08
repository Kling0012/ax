from pathlib import Path
import shutil

import yaml


SOURCE = Path(__file__).resolve().parents[2]


def test_registry_graphs_through_control_api(control_env, monkeypatch):
    env = control_env
    root = env['root']
    for name in ('change', 'release'):
        shutil.copytree(SOURCE / 'plugins' / 'subloop' / name,
                        root / 'plugins' / 'subloop' / name)
    shutil.copytree(SOURCE / 'plugins' / 'nodes', root / 'plugins' / 'nodes')
    shutil.copytree(SOURCE / 'plugins' / 'subloop' / '_lib', root / 'plugins' / 'subloop' / '_lib')
    registry = root / 'plugins' / 'registry.yaml'
    entries = [{'id': n, 'kind': 'subloop', 'path': f'plugins/subloop/{n}/manifest.yaml'}
               for n in ('change', 'release')]
    entries.append({'id': 'resources', 'kind': 'resource',
                    'path': 'plugins/resource/resources/manifest.yaml'})
    shutil.copytree(SOURCE / 'plugins' / 'resource' / 'resources',
                    root / 'plugins' / 'resource' / 'resources')
    registry.write_text(yaml.safe_dump({'schema_version': 1, 'plugins': entries}))
    env['service'].register()
    for name in ('change', 'release'):
        view = env['core'].get(f'/v1/plugins/{name}').json()
        assert view['status'] == 'registered', view
    assert env['core'].get('/v1/plugins/resources').json()['status'] == 'rejected'

    graph = root / 'plugins' / 'subloop' / 'release' / 'graph.yaml'
    body = yaml.safe_load(graph.read_text())
    body['nodes'][0]['type'] = 'unknown.example'
    graph.write_text(yaml.safe_dump(body))
    env['service'].register()
    assert env['core'].get('/v1/plugins/release').json()['status'] == 'rejected'
    assert env['core'].get('/v1/plugins/change').json()['status'] == 'registered'

    from runtime.subloop import graphcheck
    monkeypatch.setattr(graphcheck, '_register_runtime_node_types',
                        lambda *_: graphcheck.Finding('runtime_registry_unavailable', 'hook failed'))
    env['service'].register()
    rejected = env['core'].get('/v1/plugins/change').json()
    assert rejected['status'] == 'rejected'

    entries[0]['path'] = 'plugins/subloop/release/../change/../../../../outside/manifest.yaml'
    registry.write_text(yaml.safe_dump({'schema_version': 1, 'plugins': entries}))
    env['service'].register()
    rejected = env['core'].get('/v1/plugins/change').json()
    assert rejected['status'] == 'rejected'
    assert 'パス' in rejected['note']
