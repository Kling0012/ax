"""HTTP acceptance tests with fake providers and a real SQLite database."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from core.resources.envs.sandbox.free_guard import FreeMeasurement
from core.resources.envs.sandbox.http import build_routes
from core.resources.envs.sandbox.probe_common import ProviderState, apply_gate_key
from core.resources.envs.sandbox.sqlite_gate import SQLiteGate

AUTH = '1' * 64
HEADERS = {'Authorization': 'Bearer ' + AUTH}


class FakeProvider:
    capabilities = {'web-gui'}
    key = b'fake-worker-key'
    measurement = FreeMeasurement(1, 1, 0, 100, 100, 0)

    def read_worker_key(self):
        return self.key

    def measure(self):
        return self.measurement


def make_gate(path, provider):
    return SQLiteGate(path, auth_digest=hashlib.sha256(AUTH.encode()).hexdigest(),
                      providers={'box': provider}, clock=lambda: 1000)


def payload(job):
    return {'job_id': job, 'role': 'research', 'sensitivity': 'public',
            'tools': ['web-gui'], 'budget_min': 20}


def make_ready(gate):
    env = ProviderState('box')
    env.record_ax_probe(True)
    env.complete_j_trial(success=True, trial_for='box')
    apply_gate_key(env, 'ok', 1000)
    env.complete_g_trial(success=True, trial_for='box', now=1000, max_age_s=300)
    gate.save_state(env)


def client(gate):
    return TestClient(Starlette(routes=build_routes(gate)))


def test_state_and_token_survive_restart(tmp_path):
    path = tmp_path / 'gate.sqlite'
    provider = FakeProvider()
    gate = make_gate(path, provider)
    make_ready(gate)
    with client(gate) as c:
        response = c.post('/v1/envs/box/gate-token', headers=HEADERS, json=payload('one'))
    assert response.status_code == 201
    token = response.json()['token']
    restarted = make_gate(path, provider)
    assert restarted.get_state('box').gate_state == 'available'
    assert restarted.validate('box', token)
    assert token.encode() not in path.read_bytes()
    with client(restarted) as c:
        busy = c.post('/v1/envs/box/gate-token', headers=HEADERS, json=payload('two'))
        assert busy.status_code == 409
        revoked = c.post('/v1/envs/box/gate-token/' + response.json()['token_id'] + '/revoke', headers=HEADERS, json={'reason': 'finished'})
    assert revoked.status_code == 200
    assert not restarted.validate('box', token)


def test_wrong_credential_denied(tmp_path):
    gate = make_gate(tmp_path / 'gate.sqlite', FakeProvider())
    make_ready(gate)
    with client(gate) as c:
        result = c.post('/v1/envs/box/gate-token', headers={'Authorization': 'Bearer ' + '2' * 64}, json=payload('one'))
    assert result.status_code == 401


def test_over_free_budget_denied(tmp_path):
    provider = FakeProvider()
    provider.measurement = FreeMeasurement(1, 1, 0, 10, 100, 0)
    gate = make_gate(tmp_path / 'gate.sqlite', provider)
    make_ready(gate)
    with client(gate) as c:
        result = c.post('/v1/envs/box/gate-token', headers=HEADERS, json=payload('one'))
    assert result.status_code == 403
    assert result.json()['error']['code'] == 'quota'


def test_concurrent_requests_only_one_gets_slot(tmp_path):
    path = tmp_path / 'gate.sqlite'
    provider = FakeProvider()
    make_ready(make_gate(path, provider))
    barrier = Barrier(2)
    def attempt(job):
        gate = make_gate(path, provider)
        with client(gate) as c:
            barrier.wait(timeout=10)
            return c.post('/v1/envs/box/gate-token', headers=HEADERS, json=payload(job)).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(attempt, ['one', 'two']))
    assert sorted(statuses) == [201, 409]
