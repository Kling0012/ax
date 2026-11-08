from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from axcommon.creds import TokenTable, sha256_hex
from starlette.testclient import TestClient

from core.resources.app import Config, build
from core.resources.core_auth import CoreAuth


def test_environment_lease_lifecycle(tmp_path):
    auth = CoreAuth(TokenTable({'control': sha256_hex('control-test'),
                                'worker-pool': sha256_hex('pool-test'),
                                'release-review': sha256_hex('review-test')}))
    app, _, _ = build(Config(db=str(tmp_path / 'resources.sqlite'),
                             models_file=str(tmp_path / 'missing.yaml')),
                      auth=auth, run_heartbeat_loop=False)
    worker = {'Authorization': 'Bearer pool-test'}
    control = {'Authorization': 'Bearer control-test'}
    body = {'env_id': 'env-a', 'holder': 'job-a', 'kind': 'env-job'}
    with TestClient(app) as client:
        acquire = lambda data=body: client.post('/v1/environment-leases/acquire', json=data, headers=worker)
        cap = lambda value: client.put('/v1/slots/cap', json={'kind': 'environment', 'key': 'env-a', 'cap': value}, headers=control)
        assert client.post('/v1/environment-leases/acquire', json=body).status_code == 401
        assert client.post('/v1/environment-leases/acquire', json=body, headers={'Authorization': 'Bearer review-test'}).status_code == 403
        assert acquire({**body, 'kind': 'other'}).status_code == 400
        assert cap(-1).status_code == 400
        first = acquire()
        assert first.status_code == 200
        lease = first.json()['lease_id']
        assert acquire({**body, 'kind': 'sandbox'}).status_code == 409
        assert client.post('/v1/environment-leases/release', json={'lease_id': lease, 'outcome': 'completed'}, headers=worker).json()['released']
        second = acquire({**body, 'kind': 'sandbox'})
        assert second.status_code == 200
        lease = second.json()['lease_id']
        assert client.post('/v1/environment-leases/hold-unknown', json={'lease_id': lease}, headers=worker).json()['held']
        assert acquire().status_code == 409
        assert client.post('/v1/environment-leases/release', json={'lease_id': lease, 'outcome': 'failed'}, headers=worker).json()['released']
        assert cap(0).status_code == 200
        assert acquire().status_code == 409
        assert cap(None).status_code == 200
        assert acquire().status_code == 200


def test_concurrent_environment_acquire_across_http_apps(tmp_path):
    auth = CoreAuth(TokenTable({'worker-pool': sha256_hex('pool-test')}))
    config = Config(db=str(tmp_path / 'resources.sqlite'), models_file=str(tmp_path / 'missing.yaml'))
    first, _, _ = build(config, auth=auth, run_heartbeat_loop=False)
    second, _, _ = build(config, auth=auth, run_heartbeat_loop=False)
    barrier = Barrier(2)
    with TestClient(first) as a, TestClient(second) as b:
        def acquire(pair):
            client, holder = pair
            barrier.wait(timeout=10)
            return client.post('/v1/environment-leases/acquire',
                json={'env_id': 'env-concurrent', 'holder': holder, 'kind': 'env-job'},
                headers={'Authorization': 'Bearer pool-test'}).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(acquire, [(a, 'a'), (b, 'b')]))
        assert sorted(statuses) == [200, 409]
