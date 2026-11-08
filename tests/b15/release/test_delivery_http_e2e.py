"""Release delivery through the real subloop HTTP boundary and persistent store."""
from concurrent.futures import ThreadPoolExecutor

from starlette.testclient import TestClient

from plugins.subloop.release.core.service import ReleaseService
from plugins.subloop.release.core.store import Store
from runtime.subloop.app import build_app
from tests.b7.exhelp import graph, make_executor, manifest, node
from tests.b15.conftest import merged
from tests.fakes.b7 import make_env


def test_delivery_auth_concurrency_restart_and_lost_response(tmp_path, repo_root, fake_clock, products, ports, monkeypatch):
    env = make_env(tmp_path, repo_root, fake_clock)
    env.svc.plugin_id = 'release'
    ex = make_executor(env, graph([node('idle')], []), manifest())
    monkeypatch.setattr('runtime.subloop.app.creds.read_secret_optional',
                        lambda name: 'change-token' if name == 'plugin-token-change' else None)
    path = tmp_path / 'release.db'
    store = Store(path)
    env.svc.release_service = ReleaseService(products, store, ports)
    app = build_app(ex, apply_token='control-token', run_loops=False)
    headers = {'Authorization': 'Bearer change-token'}
    payload = merged()
    payload['issues'] = [{'number': 10, 'issue_repo': 'public/issues'}]
    request = {'delivery_id': 'release-delivery-1', 'destination': 'release', 'payload': payload}
    lookup = {'delivery_id': request['delivery_id'], 'destination': 'release'}
    with TestClient(app) as client:
        assert client.post('/release/deliveries', json=request).status_code == 401
        assert client.post('/release/deliveries', json=request, headers={'Authorization': 'Bearer other-component'}).status_code == 401
        assert client.post('/release/deliveries', json=request, headers={'Authorization': 'Bearer control-token'}).status_code == 401
        assert client.post('/apply', json={'mode': 'run', 'revision': 1}, headers=headers).status_code == 401
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: client.post('/release/deliveries', json=request, headers=headers), range(8)))
        assert all(response.status_code == 200 for response in responses)
        receipt = responses[0].json()
        assert all(response.json() == receipt for response in responses)
        assert receipt['repo'] == payload['repo'] and receipt['pr'] == 1
        assert receipt['merge_sha'] == payload['merge_sha'] and receipt['merged_at'] == payload['merged_at']
        changed = {**request, 'payload': {**payload, 'merge_sha': 'b' * 40}}
        assert client.post('/release/deliveries', json=changed, headers=headers).status_code == 409
        assert client.post('/release/deliveries', json={**changed, 'delivery_id': 'other'}, headers=headers).status_code == 409
        assert len(store.items()) == 1
    store.close()
    store = Store(path)
    env.svc.release_service = ReleaseService(products, store, ports)
    app = build_app(ex, apply_token='control-token', run_loops=False)
    with TestClient(app) as client:
        # The caller discards the response and reconciles after restart.
        assert client.post('/release/deliveries/lookup', json=lookup, headers=headers).json() == receipt
        assert client.post('/release/deliveries', json=request, headers=headers).json() == receipt
        assert client.post('/release/deliveries/lookup', json={**lookup, 'delivery_id': 'unknown'}, headers=headers).json()['status'] == 'unknown'
        assert len(store.items()) == 1
        assert store.item(payload['repo'], 1)['issues'][0]['issue_repo'] == 'public/issues'
        assert client.post('/release/deliveries', json={**request, 'delivery_id': 'missing-paths',
                           'payload': {**payload, 'changed_paths': None}}, headers=headers).status_code == 422
    store.close()
