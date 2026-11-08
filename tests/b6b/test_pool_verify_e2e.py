"""Change HTTP to worker pool to the real launcher CLI and fake Docker."""
import importlib
import json

from starlette.testclient import TestClient
from axcommon.creds import sha256_hex
from tests.b6b.test_verify_launcher_e2e import verify_system


def test_verification_http_uses_real_launcher_cli(verify_system, tmp_path, monkeypatch):
    sb, _, head, request = verify_system
    tokens = tmp_path / 'change-tokens'
    tokens.write_text('change ' + sha256_hex('change-test') + '\n')
    monkeypatch.setenv('AX_PLUGIN_TOKENS', str(tokens))
    launcher_module = importlib.import_module('plugins.resource.worker-pool.launcher_client')
    calls = []
    def transport(command, stdin):
        calls.append(command)
        original = command.removeprefix('ax-env-run --caller launcher ')
        result = sb.subprocess_env_run(original, '--caller', 'launcher', stdin=stdin)
        return result.returncode, result.stdout
    launcher = launcher_module.LauncherClient(transport=transport)
    pool = importlib.import_module('plugins.resource.worker-pool.app')
    _, app, svc = pool.build(pool.Config(db=str(tmp_path / 'pool.sqlite')), launcher=launcher,
                             run_heartbeat_loop=False, emit=lambda *a, **k: None)
    client = TestClient(app, headers={'Authorization': 'Bearer change-test'})
    body = request(head)
    recipe = client.post('/v1/verification/recipes/http-recipe', json={k: body[k] for k in ('project', 'target', 'recipe_id')})
    assert recipe.status_code == 200, recipe.text
    body['recipe_snapshot_id'] = recipe.json()['recipe_snapshot_id']
    path = '/v1/verification/jobs/http-job'
    result = client.post(path, json=body)
    assert result.status_code == 200, result.text
    assert result.json()['state'] == 'running' and result.json()['revision'] == head
    sb.fd.finish('ax-verify-http-job', exit_code=0, logs='output\n')
    result = client.get(path)
    assert result.status_code == 200, result.text
    assert result.json()['state'] == 'exited' and result.json()['log_complete'] is True
    assert client.post(path, json=body).status_code == 200
    assert sum('job-verify-prepare' in call for call in calls) == 1
    assert client.post(path, json={**body, 'run_id': 'other-run'}).status_code == 409
    svc.store.close()
