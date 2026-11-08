from datetime import timedelta

from pathlib import Path

from axcommon.creds import TokenTable, sha256_hex
from core.resources.core_auth import CoreAuth
from starlette.testclient import TestClient

from axcommon import timeutil
from axcommon.client import ApiClient
from core.control.app import create_core_app
from core.control.selection_exclusions import ExclusionAdapter, ResourcesExclusionClient
from core.control.service import ControlService
from core.control.store import Store as ControlStore
from core.resources.app import Config, build
from core.resources.store import Store as ResourceStore
from tests.b3.conftest import TOK_INTAKE, TOK_MANAGER
from tests.fakes.http import FakeTransport


def test_configured_exclusion_and_limit_survive_both_restarts(control_env):
    env = control_env
    manifest = Path(__file__).resolve().parents[2] / 'plugins/resource/worker-pool/manifest.yaml'
    (env['root'] / 'plugins/resource/worker-pool/manifest.yaml').write_text(manifest.read_text())
    env['service'].register()
    path = env['tmp_path'] / 'resources.sqlite'
    store = ResourceStore(str(path))
    store.seed([{'id': 'model-a', 'kind': 'model', 'family': 'family'}])
    cfg = Config(db=str(path), ext_dir=str(env['tmp_path'] / 'no-ext'))
    auth = CoreAuth(TokenTable({'control': sha256_hex('control-test')}))
    app, _, _ = build(cfg, store=store, auth=auth, run_heartbeat_loop=False)
    resources = TestClient(app)
    transport = FakeTransport()

    def forward(call):
        response = resources.request(call.method, call.path, content=call.body, headers=call.headers)
        return response.status_code, dict(response.headers), response.content

    transport.add('POST', '/v1/command-exclusions', handler=forward)
    transport.add('GET', '/v1/resources', handler=forward)
    transport.add('GET', '/v1/slots/effective-limits', handler=forward)
    transport.add('PUT', '/v1/slots/effective-limits', handler=forward)
    limits_client = ApiClient('http://resources', token='control-test', transport=transport)
    env['service'].resources_client = limits_client
    adapter = ExclusionAdapter(ResourcesExclusionClient(ApiClient('http://resources', token='control-test', transport=transport)))
    env['service'].exclusion_adapter = adapter
    until = timeutil.format_ts(timeutil.now() + timedelta(minutes=20))
    manager = {'Authorization': 'Bearer ' + TOK_MANAGER}
    user = {'Authorization': 'Bearer ' + TOK_INTAKE}
    exclusion = {'target': 'worker-pool', 'op': 'exclude_resource',
                 'args': {'resource_id': 'model-a', 'until': until},
                 'expires_at': until, 'on_expiry': 'release', 'reason': 'test'}
    knob = {'target': 'worker-pool', 'op': 'set_knob', 'args': {'name': 'family_cap', 'value': {'family': 2}},
            'issued_by': 'user', 'reason': 'test'}
    assert env['core'].post('/v1/commands', json=knob, headers=user).status_code == 201
    assert env['core'].post('/v1/commands', json=exclusion, headers=manager).status_code == 201
    before_status = resources.get('/v1/resources/model-a').json()['status']
    before_caps = resources.get('/v1/slots').json()['families']
    resources.close()
    store.close()
    store = ResourceStore(str(path))
    app, _, _ = build(cfg, store=store, auth=auth, run_heartbeat_loop=False)
    resources = TestClient(app)
    restarted = ControlService(ControlStore(env['service'].store.path), None, exclusion_adapter=adapter, resources_client=limits_client,
                               registry_root=env['root'], registry_file=env['root'] / 'plugins' / 'registry.yaml')
    try:
        with TestClient(create_core_app(restarted)) as control:
            assert control.post('/v1/sweep', json={}, headers=manager).status_code == 200
            assert control.get('/v1/plugins/worker-pool/effective').json()['knobs']['family_cap'] == {'family': 2}
            after_status = resources.get('/v1/resources/model-a').json()['status']
            after_caps = resources.get('/v1/slots').json()['families']
        assert before_status == after_status == 'excluded'
        assert before_caps['family']['cap'] == after_caps['family']['cap'] == 2
    finally:
        restarted.store.close()
        resources.close()
        store.close()
