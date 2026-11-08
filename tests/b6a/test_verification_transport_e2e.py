"""Authenticated change-to-host verification job transport."""
import importlib
import threading
from concurrent.futures import ThreadPoolExecutor

from starlette.testclient import TestClient
from axcommon.creds import sha256_hex

BODY = {'project': 'demo', 'target': 'repo', 'revision': 'a' * 40, 'run_id': 'run-one',
        'attempt': 'base', 'recipe_id': 'recipe', 'recipe_snapshot_id': 'vs-' + 'a' * 32, 'stage': 'verify', 'step': 'test'}


class Launcher:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.lost = False
        self.lock = threading.Lock()
        self.recipe_lost = False
        self.recipe_calls = 0
        self.recipe_bad = False
        self.recipe_isolation = True
        self.recipe_hosts = []
    def job_verify_recipe(self, rid, req):
        self.recipe_calls += 1
        if self.recipe_lost:
            raise OSError('transport lost')
        return {**req, 'request_id': rid, 'recipe_snapshot_id': 'vs-' + 'a' * 32,
                'recipe_version': 'v1', 'allow_hosts': self.recipe_hosts, 'prestart_isolation_enforced': self.recipe_isolation, **({'target': 'wrong'} if self.recipe_bad else {})}
    def job_verify_prepare(self, jid, req):
        with self.lock:
            self.calls.append(jid)
        self.results[jid] = {'job_id': jid, **req, 'recipe_version': 'v1', 'runner': 'verify-python',
            'command': 'trusted test', 'workdir_id': 'work', 'network_mode': 'none', 'allow_hosts': [],
            'isolation_verified': True, 'home_absent': True, 'credentials_absent': True,
            'host_config_absent': True, 'network_enforced': True, 'started_at': None, 'ended_at': None, 'state': 'running',
            'exit_code': None, 'oom_killed': None, 'timed_out': None, 'executed': False,
            'log_complete': False, 'output_truncated': False}
        if self.lost:
            raise OSError('transport lost')
        return {'job_id': jid, 'state': 'running'}
    def job_verify_result(self, jid):
        return self.results[jid]


def test_verification_transport_scope_ownership_and_lost_response(tmp_path, monkeypatch):
    tokens = tmp_path / 'plugin-tokens'
    tokens.write_text('change ' + sha256_hex('change-test') + '\nrelease ' + sha256_hex('release-test') + '\n')
    monkeypatch.setenv('AX_PLUGIN_TOKENS', str(tokens))
    pool = importlib.import_module('plugins.resource.worker-pool.app')
    launcher = Launcher()
    _, work, service = pool.build(pool.Config(db=str(tmp_path / 'pool.sqlite')), launcher=launcher,
                                  run_heartbeat_loop=False, emit=lambda *a, **k: None)
    client = TestClient(work, headers={'Authorization': 'Bearer change-test'})
    assert client.get('/v1/capabilities').json() == {'change_safety': {'version': 1, 'tool_free_required': True,
        'runner': 'chat-json', 'host_profile': 'tool-free-chat-v1'}}
    recipe = '/v1/verification/recipes/recipe-one'
    binding = {'project': 'demo', 'target': 'repo', 'recipe_id': 'recipe'}
    assert client.get(recipe).status_code == 404
    assert client.post(recipe, headers={'Authorization': 'Bearer release-test'}, json=binding).status_code == 404
    assert client.post(recipe, json={**binding, 'network': 'host'}).status_code == 422
    assert client.post('/v1/verification/jobs/unknown-snapshot', json=BODY).status_code == 404
    launcher.recipe_lost = True
    assert client.post(recipe, json=binding).status_code == 503
    launcher.recipe_lost = False
    launcher.recipe_bad = True
    assert client.get(recipe).status_code == 503
    launcher.recipe_bad = False
    launcher.recipe_isolation = 1
    assert client.get(recipe).status_code == 503
    launcher.recipe_isolation = True
    assert client.get(recipe).json()['recipe_snapshot_id'] == BODY['recipe_snapshot_id']
    assert client.post(recipe, json=binding).status_code == 200
    assert client.post(recipe, json={**binding, 'target': 'other'}).status_code == 409
    assert client.post('/v1/verification/jobs/wrong-scope', json={**BODY, 'target': 'other'}).status_code == 404
    url = '/v1/verification/jobs/verify-one'
    assert client.get(url).status_code == 404
    assert client.post(url, headers={'Authorization': 'Bearer release-test'}, json=BODY).status_code == 404
    assert client.post(url, json={**BODY, 'command': 'unsafe'}).status_code == 422
    launcher.lost = True
    assert client.post(url, json=BODY).status_code == 503
    launcher.lost = False
    assert client.get(url).json()['job_id'] == 'verify-one'
    assert client.post(url, json=BODY).status_code == 200
    assert launcher.calls == ['verify-one']
    assert client.post(url, json={**BODY, 'revision': 'b' * 40}).status_code == 409
    assert client.get(url, headers={'Authorization': 'Bearer release-test'}).status_code == 404
    launcher.results['verify-one']['allow_hosts'] = ['unexpected.example']
    assert client.get(url).status_code == 503
    launcher.results['verify-one']['allow_hosts'] = []
    launcher.results['verify-one']['target'] = 'wrong'
    assert client.get(url).status_code == 503
    launcher.results['verify-one']['target'] = 'repo'
    launcher.results['verify-one'].pop('runner')
    assert client.get(url).status_code == 503
    with ThreadPoolExecutor(max_workers=4) as executor:
        replies = list(executor.map(lambda _: client.post('/v1/verification/jobs/verify-two', json=BODY), range(4)))
    assert all(r.status_code in (200, 503) for r in replies)
    assert launcher.calls.count('verify-two') == 1
    service.store.close()


def test_recipe_recovers_after_pool_restart_and_concurrent_posts(tmp_path, monkeypatch):
    tokens = tmp_path / 'plugin-tokens'
    tokens.write_text('change ' + sha256_hex('change-test') + '\n')
    monkeypatch.setenv('AX_PLUGIN_TOKENS', str(tokens))
    pool = importlib.import_module('plugins.resource.worker-pool.app')
    launcher = Launcher()
    config = pool.Config(db=str(tmp_path / 'pool.sqlite'))
    _, app, service = pool.build(config, launcher=launcher, run_heartbeat_loop=False, emit=lambda *a, **k: None)
    client = TestClient(app, headers={'Authorization': 'Bearer change-test'})
    path = '/v1/verification/recipes/restart-recipe'
    binding = {'project': 'demo', 'target': 'repo', 'recipe_id': 'recipe'}
    launcher.recipe_lost = True
    assert client.post(path, json=binding).status_code == 503
    service.store.close()
    _, app, service = pool.build(config, launcher=launcher, run_heartbeat_loop=False, emit=lambda *a, **k: None)
    client = TestClient(app, headers={'Authorization': 'Bearer change-test'})
    launcher.recipe_lost = False
    with ThreadPoolExecutor(max_workers=4) as executor:
        replies = list(executor.map(lambda _: client.post(path, json=binding), range(4)))
    assert all(r.status_code == 200 for r in replies)
    assert len({r.json()['recipe_snapshot_id'] for r in replies}) == 1
    assert client.get(path).status_code == 200
    assert client.post('/v1/verification/recipes/other-recipe', json=binding).status_code == 503
    service.store.close()


def test_recipe_allowlist_is_bound_to_dependency_only(tmp_path, monkeypatch):
    tokens = tmp_path / 'plugin-tokens'
    tokens.write_text('change ' + sha256_hex('change-test') + '\n')
    monkeypatch.setenv('AX_PLUGIN_TOKENS', str(tokens))
    pool = importlib.import_module('plugins.resource.worker-pool.app')
    launcher = Launcher()
    launcher.recipe_hosts = ['packages.example.org']
    _, work, service = pool.build(pool.Config(db=str(tmp_path / 'pool.sqlite')), launcher=launcher,
                                  run_heartbeat_loop=False, emit=lambda *a, **k: None)
    client = TestClient(work, headers={'Authorization': 'Bearer change-test'})
    assert client.post('/v1/verification/recipes/recipe-hosts', json={'project': 'demo', 'target': 'repo', 'recipe_id': 'recipe'}).status_code == 200
    path = '/v1/verification/jobs/verify-offline'
    assert client.post(path, json=BODY).status_code == 200
    launcher.results['verify-offline']['allow_hosts'] = launcher.recipe_hosts
    assert client.get(path).status_code == 503
    launcher.results['verify-offline']['allow_hosts'] = []
    launcher.results['verify-offline']['network_mode'] = 'allowlist'
    assert client.get(path).status_code == 503
    dependency = {**BODY, 'stage': 'dependency'}
    path = '/v1/verification/jobs/dependency-network'
    assert client.post(path, json=dependency).status_code == 503
    launcher.results['dependency-network']['network_mode'] = 'allowlist'
    launcher.results['dependency-network']['allow_hosts'] = launcher.recipe_hosts
    assert client.get(path).status_code == 200
    launcher.results['dependency-network']['allow_hosts'] = []
    assert client.get(path).status_code == 503
    service.store.close()
