import time
"""Exercise startup with an empty registry and unrelated legacy pool files."""
import importlib
from pathlib import Path

import yaml
from starlette.testclient import TestClient
from core.resources.app import Config, build

ROOT = Path(__file__).resolve().parents[2]


def test_empty_registry_startup_ignores_legacy_files(tmp_path, monkeypatch):
    from axcommon.creds import sha256_hex
    table = tmp_path / "pool-core-tokens"
    table.write_text("intake " + sha256_hex("test-intake") + "\n")
    monkeypatch.setenv("AX_CORE_TOKENS", str(table))
    old = tmp_path / '.claude' / 'worker-pool'
    old.mkdir(parents=True)
    for name in ('routing.json', 'profiles.json', 'limits.json'):
        (old / name).write_text('{"models":{"legacy/poison":{"cap":0,"level":"L4"}}}')
    monkeypatch.setenv('HOME', str(tmp_path))
    resource_app, _, service = build(Config(db=str(tmp_path / 'resources.sqlite'),
        models_file=str(ROOT / 'config/models.example.yaml')), run_heartbeat_loop=False)
    pool = importlib.import_module('plugins.resource.worker-pool.app')
    selection = importlib.import_module('plugins.resource.worker-pool.selection')
    pool_app, _, _ = pool.build(pool.Config(db=str(tmp_path / 'jobs.sqlite')), run_heartbeat_loop=False)
    manifest = yaml.safe_load((ROOT / 'plugins/resource/worker-pool/manifest.yaml').read_text())
    with TestClient(resource_app) as resources, TestClient(pool_app, headers={"Authorization": "Bearer test-intake"}) as worker:
        health = resources.get('/healthz').json()
        assert health['seed_error'] is None and health['seeded'] == 0
        rows = service.store.list()
        assert rows == []
        assert selection.slots_for(rows, [], selection.Knobs())['model_cap'] == {}
        assert worker.get('/v1/slots').json()['in_use'] == 0
        assert worker.get('/status').status_code == 200
        assert manifest['knobs']['family_cap']['default'] == ''
        assert manifest['knobs']['model_cap']['default'] == ''

    # A user-registered model is loaded from AX configuration only.
    configured = tmp_path / 'models.yaml'
    configured.write_text(yaml.safe_dump({'models': [{'id': 'example/model', 'kind': 'model',
        'family': 'example', 'tier': 'free', 'billing': 'flat', 'level': 'L1',
        'vision': False, 'runner': 'agent-cli', 'cap': 2, 'status': 'active'}]}))
    configured_app, _, configured_service = build(Config(db=str(tmp_path / 'configured.sqlite'),
        models_file=str(configured)), run_heartbeat_loop=False)
    with TestClient(configured_app) as client:
        assert client.get('/healthz').json()['seeded'] == 1
        rows = configured_service.store.list()
        assert [row['id'] for row in rows] == ['example/model']
        caps = selection.slots_for(rows, [], selection.Knobs(family_cap={'example': 3}))
        assert caps['model_cap'] == {'example/model': 2}
        assert caps['family_cap'] == {'example': 3, 'free': 2}


def test_resource_cap_reservation_api(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from axcommon.creds import TokenTable, sha256_hex
    from core.resources.core_auth import CoreAuth

    configured = tmp_path / 'models.yaml'
    configured.write_text(yaml.safe_dump({'models': [{'id': 'example/model', 'kind': 'model',
        'family': 'example', 'tier': 'standard', 'billing': 'flat', 'level': 'L1',
        'vision': False, 'runner': 'agent-cli', 'status': 'active'}]}))
    auth = CoreAuth(TokenTable({'control': sha256_hex('control-test'), 'worker-pool': sha256_hex('pool-test'), 'release-review': sha256_hex('review-test')}))
    app, _, _ = build(Config(db=str(tmp_path / 'resources.sqlite'), models_file=str(configured)),
                      auth=auth, run_heartbeat_loop=False)
    control = {'Authorization': 'Bearer control-test'}
    worker = {'Authorization': 'Bearer pool-test'}
    reviewer = {'Authorization': 'Bearer review-test'}
    with TestClient(app) as client:
        def effective(revision, model_cap, family_cap):
            assert client.put('/v1/slots/effective-limits', json={'revision': revision, 'expires_at': time.time() + 3600, 'models': {'example/model': model_cap}, 'families': {'example': family_cap}}, headers=control).status_code == 200
        effective(1, 1, 1)
        assert client.post('/v1/slots/reserve', json={'reservation_id': 'job-other', 'model': 'example/model'}, headers=reviewer).status_code == 403
        assert client.post('/v1/slots/release', json={'reservation_id': 'job-other'}, headers=reviewer).status_code == 403
        assert client.post('/v1/slots/reserve', json={'reservation_id': 'publish-review-fixture', 'model': 'example/model'}, headers=reviewer).json()['reserved']
        assert client.post('/v1/slots/release', json={'reservation_id': 'publish-review-fixture'}, headers=reviewer).json()['released']
        assert client.get('/v1/slots').json()['models']['example/model']['cap'] == 1
        assert client.put('/v1/slots/cap', json={'kind': 'model', 'key': 'example/model', 'cap': 2}, headers=worker).status_code == 403
        for invalid in (-1, True, 1.5, '2'):
            assert client.put('/v1/slots/cap', json={'kind': 'model', 'key': 'example/model', 'cap': invalid}, headers=control).status_code == 400
        for kind, key, cap in [('model', 'example/model', 2), ('family', 'example', 2)]:
            assert client.put('/v1/slots/cap', json={'kind': kind, 'key': key, 'cap': cap}, headers=control).status_code == 200
        effective(2, 2, 2)
        def reserve(i):
            return client.post('/v1/slots/reserve', json={'reservation_id': f'job-{i}', 'model': 'example/model'}, headers=worker).status_code
        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(reserve, range(8)))
        assert statuses.count(200) == 2 and statuses.count(409) == 6
        slots = client.get('/v1/slots').json()
        assert slots['families']['example'] == {'cap': 2, 'used': 2}
        assert slots['models']['example/model']['used'] == 2
        held = [i for i, status in enumerate(statuses) if status == 200]
        assert client.post('/v1/slots/release', json={'reservation_id': f'job-{held[0]}'}, headers=worker).json()['released']
        assert reserve(20) == 200
        assert client.put('/v1/slots/cap', json={'kind': 'model', 'key': 'example/model', 'cap': 0}, headers=control).status_code == 200
        assert client.post('/v1/slots/release', json={'reservation_id': f'job-{held[1]}'}, headers=worker).status_code == 200
        assert client.post('/v1/slots/release', json={'reservation_id': 'job-20'}, headers=worker).status_code == 200
        effective(3, 0, 2)
        assert reserve(21) == 409

        # Separate SQLite connections exercise the cross-process transaction boundary.
        from core.resources.store import Store
        from threading import Barrier
        assert client.put('/v1/slots/cap', json={'kind': 'model', 'key': 'example/model', 'cap': 2}, headers=control).status_code == 200
        effective(4, 2, 2)
        barrier = Barrier(8)
        def concurrent_reserve(i):
            connection = Store(tmp_path / 'resources.sqlite')
            try:
                barrier.wait(timeout=10)
                return connection.reserve_slot(f'parallel-{i}', 'example/model')
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=8) as executor:
            concurrent = list(executor.map(concurrent_reserve, range(8)))
        assert concurrent.count(True) == 2 and concurrent.count(False) == 6


def test_family_cap_defaults_and_boundaries_via_http(tmp_path):
    from axcommon.creds import TokenTable, sha256_hex
    from core.resources.core_auth import CoreAuth

    configured = tmp_path / 'models.yaml'
    configured.write_text(yaml.safe_dump({'models': [
        {'id': f'example/model-{i}', 'kind': 'model', 'family': 'example',
         'tier': 'standard', 'billing': 'flat', 'level': 'L1', 'vision': False,
         'runner': 'agent-cli', 'status': 'active'} for i in (1, 2)
    ]}))
    auth = CoreAuth(TokenTable({'control': sha256_hex('control-test'),
                                'worker-pool': sha256_hex('pool-test')}))
    app, _, _ = build(Config(db=str(tmp_path / 'resources.sqlite'), models_file=str(configured)),
                      auth=auth, run_heartbeat_loop=False)
    control = {'Authorization': 'Bearer control-test'}
    worker = {'Authorization': 'Bearer pool-test'}
    with TestClient(app) as client:
        revision = [0]
        def effective(value):
            revision[0] += 1
            assert client.put('/v1/slots/effective-limits', json={'revision': revision[0], 'expires_at': time.time() + 3600, 'models': {'example/model-1': 1, 'example/model-2': 1}, 'families': {'example': value}}, headers=control).status_code == 200
        effective(1)
        def cap(value):
            response = client.put('/v1/slots/cap', json={'kind': 'family', 'key': 'example', 'cap': value}, headers=control)
            if response.status_code == 200:
                effective(1 if value is None else value)
            return response
        def reserve(reservation_id, model):
            return client.post('/v1/slots/reserve', json={'reservation_id': reservation_id, 'model': model}, headers=worker)
        def release(reservation_id):
            return client.post('/v1/slots/release', json={'reservation_id': reservation_id}, headers=worker)

        assert client.get('/v1/slots').json()['families']['example'] == {'cap': 1, 'used': 0}
        assert reserve('first', 'example/model-1').status_code == 200
        assert reserve('second', 'example/model-2').status_code == 409
        assert cap(-1).status_code == 400
        assert cap(0).status_code == 200
        assert client.get('/v1/slots').json()['families']['example'] == {'cap': 0, 'used': 1}
        assert release('first').json()['released'] is True
        assert reserve('third', 'example/model-2').status_code == 409
        assert cap(None).status_code == 200
        assert client.get('/v1/slots').json()['families']['example'] == {'cap': 1, 'used': 0}
        assert reserve('fourth', 'example/model-2').status_code == 200
        assert reserve('fifth', 'example/model-1').status_code == 409


def test_effective_limits_api_persistence_and_concurrency(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from axcommon.creds import TokenTable, sha256_hex
    from core.resources.core_auth import CoreAuth

    configured = tmp_path / 'models.yaml'
    configured.write_text(yaml.safe_dump({'models': [
        {'id': f'model-{i}', 'kind': 'model', 'family': 'family', 'tier': 'standard', 'billing': 'flat', 'level': 'L1', 'vision': False, 'runner': 'agent-cli', 'status': 'active'}
        for i in range(2)]}))
    auth = CoreAuth(TokenTable({'control': sha256_hex('control-test'), 'worker-pool': sha256_hex('pool-test')}))
    config = Config(db=str(tmp_path / 'resources.sqlite'), models_file=str(configured))
    control = {'Authorization': 'Bearer control-test'}
    worker = {'Authorization': 'Bearer pool-test'}
    app, _, service = build(config, auth=auth, run_heartbeat_loop=False)
    def limits(revision, expires, models=None, families=None):
        return {'revision': revision, 'expires_at': expires,
                'models': models if models is not None else {'model-0': 2, 'model-1': 2},
                'families': families if families is not None else {'family': 2}}
    def reserve(client, ident, model='model-0'):
        return client.post('/v1/slots/reserve', headers=worker, json={'reservation_id': ident, 'model': model})
    with TestClient(app) as client:
        assert reserve(client, 'missing').json()['reason'] == 'limits_missing'
        initial = limits(1, time.time() + 3600)
        assert client.put('/v1/slots/effective-limits', headers=worker, json=initial).status_code == 403
        assert client.put('/v1/slots/effective-limits', headers=control, json=initial).status_code == 200
        with ThreadPoolExecutor(max_workers=12) as executor:
            statuses = list(executor.map(lambda i: reserve(client, f'job-{i}', f'model-{i % 2}').status_code, range(12)))
        assert statuses.count(200) == 2 and statuses.count(409) == 10
        held = [f'job-{i}' for i, status in enumerate(statuses) if status == 200]
        zero = limits(2, time.time() + 3600, families={'family': 0})
        assert client.put('/v1/slots/effective-limits', headers=control, json=zero).status_code == 200
        assert client.get('/v1/slots').json()['families']['family']['used'] == 2
        assert reserve(client, 'zero').json()['reason'] == 'capacity_exhausted'
        assert client.put('/v1/slots/effective-limits', headers=control, json=initial).status_code == 409
        for ident in held:
            assert client.post('/v1/slots/release', headers=worker, json={'reservation_id': ident}).json()['released']
        assert reserve(client, 'zero-again').status_code == 409
        expired = limits(3, time.time() - 1)
        assert client.put('/v1/slots/effective-limits', headers=control, json=expired).status_code == 200
        assert reserve(client, 'expired').json()['reason'] == 'limits_expired'
    service.store.close()
    app2, _, service2 = build(config, auth=auth, run_heartbeat_loop=False)
    with TestClient(app2) as client:
        assert reserve(client, 'after-restart').json()['reason'] == 'limits_expired'
        assert client.put('/v1/slots/effective-limits', headers=control, json=initial).status_code == 409
        fresh = limits(4, time.time() + 3600, models={'model-0': 0, 'model-1': 2})
        assert client.put('/v1/slots/effective-limits', headers=control, json=fresh).status_code == 200
        assert reserve(client, 'model-zero').json()['reason'] == 'capacity_exhausted'
        assert reserve(client, 'other-model', 'model-1').status_code == 200
    service2.store.close()
