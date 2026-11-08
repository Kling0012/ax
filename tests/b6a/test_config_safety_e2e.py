import time

import pytest
import yaml
from starlette.testclient import TestClient

from axcommon.creds import TokenTable, sha256_hex
from core.resources.app import Config, build
from core.resources.core_auth import CoreAuth


@pytest.fixture
def resources(tmp_path):
    models = tmp_path / 'models.yaml'
    models.write_text(yaml.safe_dump({'models': [
        {'id': 'model-a', 'kind': 'model', 'family': 'family', 'tier': 'standard',
         'billing': 'flat', 'level': 'L1', 'vision': False, 'runner': 'agent-cli', 'status': 'active'},
        {'id': 'model-b', 'kind': 'model', 'family': 'family', 'tier': 'standard',
         'billing': 'flat', 'level': 'L1', 'vision': False, 'runner': 'agent-cli', 'status': 'active'}]}))
    config = Config(db=str(tmp_path / 'resources.sqlite'), models_file=str(models), ext_dir=str(tmp_path / 'ext'))
    auth = CoreAuth(TokenTable({'control': sha256_hex('control-test'), 'worker-pool': sha256_hex('pool-test')}))
    return config, auth


def limits(revision=1):
    return {'revision': revision, 'expires_at': time.time() + 3600,
            'models': {'model-a': 1, 'model-b': 1}, 'families': {'family': 2}}


CONTROL = {'Authorization': 'Bearer control-test'}
WORKER = {'Authorization': 'Bearer pool-test'}


def test_invalid_limits_leave_other_model_usable(resources):
    config, auth = resources
    app, _, service = build(config, auth=auth, run_heartbeat_loop=False)
    with TestClient(app) as client:
        assert client.put('/v1/slots/effective-limits', headers=CONTROL, json=limits()).status_code == 200
        for bad in (-1, True, 1.5, '2', None):
            update = limits(2)
            update['models']['model-a'] = bad
            response = client.put('/v1/slots/effective-limits', headers=CONTROL, json=update)
            assert response.status_code == 400
            assert response.json().get('error')
        assert client.post('/v1/slots/reserve', headers=WORKER,
                           json={'reservation_id': 'unrelated', 'model': 'model-b'}).status_code == 200
        assert client.get('/healthz').status_code == 200
        assert client.put('/v1/slots/effective-limits', headers=WORKER, json=limits(2)).status_code == 403
    service.store.close()


def test_limit_restart_stale_and_expiry(resources):
    config, auth = resources
    initial = limits(2)
    app, _, service = build(config, auth=auth, run_heartbeat_loop=False)
    with TestClient(app) as client:
        assert client.put('/v1/slots/effective-limits', headers=CONTROL, json=initial).status_code == 200
    service.store.close()
    app, _, service = build(config, auth=auth, run_heartbeat_loop=False)
    with TestClient(app) as client:
        assert client.get('/v1/slots').json()['models']['model-a']['cap'] == 1
        assert client.put('/v1/slots/effective-limits', headers=CONTROL, json=limits(1)).status_code == 409
        expired = limits(3)
        expired['expires_at'] = time.time() - 1
        assert client.put('/v1/slots/effective-limits', headers=CONTROL, json=expired).status_code == 200
        response = client.post('/v1/slots/reserve', headers=WORKER, json={'reservation_id': 'expired', 'model': 'model-a'})
        assert response.status_code == 409 and response.json()['reason'] == 'limits_expired'
    service.store.close()


@pytest.mark.parametrize('headers', [{}, WORKER], ids=['anonymous', 'worker'])
def test_command_exclusion_requires_control_authorization(resources, headers):
    config, auth = resources
    app, _, service = build(config, auth=auth, run_heartbeat_loop=False)
    with TestClient(app) as client:
        body = {'command_id': 'unauthorized', 'resource_id': 'model-a', 'active': True,
                'reason': 'test', 'until': '2099-01-01T00:00:00Z'}
        response = client.post('/v1/command-exclusions', headers=headers, json=body)
        assert response.status_code in (401, 403)
        assert client.get('/v1/resources/model-a').json()['status'] == 'active'
    service.store.close()


@pytest.mark.parametrize('headers', [{}, WORKER], ids=['anonymous', 'worker'])
def test_unauthorized_release_preserves_exclusion(resources, headers):
    config, auth = resources
    app, _, service = build(config, auth=auth, run_heartbeat_loop=False)
    body = {'command_id': 'protected', 'resource_id': 'model-a', 'active': True,
            'reason': 'test', 'until': '2099-01-01T00:00:00Z'}
    with TestClient(app) as client:
        assert client.post('/v1/command-exclusions', headers=CONTROL, json=body).status_code == 200
        response = client.post('/v1/command-exclusions', headers=headers,
                               json={'command_id': 'protected', 'resource_id': 'model-a', 'active': False})
        assert response.status_code in (401, 403)
        assert 'pool-test' not in response.text
        assert client.post('/v1/command-exclusions', headers=headers, content=b'{').status_code in (401, 403)
        assert client.get('/v1/resources/model-a').json()['status'] == 'excluded'
        assert client.post('/v1/command-exclusions', headers=CONTROL,
                           json={'command_id': 'protected', 'resource_id': 'model-a', 'active': False}).status_code == 200
        assert client.get('/v1/resources/model-a').json()['status'] == 'active'
    service.store.close()


@pytest.mark.parametrize('content', [
    'models: [broken',
    yaml.safe_dump({'models': [{'id': 'broken', 'kind': 'model', 'family': 'family',
                                'tier': 'standard', 'billing': 'flat', 'level': 'L1',
                                'vision': False, 'runner': 'agent-cli', 'status': 'active', 'cap': -1}]}),
], ids=['malformed-yaml', 'negative-cap'])
def test_bad_model_file_fails_closed_without_stopping_api(tmp_path, content):
    path = tmp_path / 'models.yaml'
    path.write_text(content)
    auth = CoreAuth(TokenTable({'worker-pool': sha256_hex('pool-test')}))
    app, _, service = build(Config(db=str(tmp_path / 'empty.sqlite'), models_file=str(path),
                                   ext_dir=str(tmp_path / 'ext')), auth=auth, run_heartbeat_loop=False)
    with TestClient(app) as client:
        health = client.get('/healthz')
        assert health.status_code == 200 and health.json()['seed_error']
        assert client.get('/v1/resources').json()['resources'] == []
        response = client.post('/v1/slots/reserve', headers=WORKER,
                               json={'reservation_id': 'bad-config', 'model': 'broken'})
        assert response.status_code != 200
    service.store.close()
