"""Public research request through launcher, verifier, pool and real ledger."""
import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from starlette.testclient import TestClient

from axcommon.client import LedgerClient
from axcommon.creds import TokenTable, sha256_hex
from core.intake.app import build_app
from core.intake.dest.b12 import work
from core.intake.dest.b12.common import Engine
from core.intake.dest.b12.common.store import Store as IntakeStore
from core.intake.vias import Ident
from core.ledger import LedgerService
from core.ledger.app import create_core_app
from core.ledger.store import Store as LedgerStore
from tests.b5.helpers import instruction, simple_dest, write_registry
from tests.b6a.test_dispatch_e2e import LauncherAdapter, system, wait_for
from tests.b6b.helpers import Sandbox
from tests.b6b.test_research_snapshot_e2e import make_repo


@pytest.mark.parametrize('invalid', [False, True])
def test_public_research_through_verified_ledger(system, tmp_path, monkeypatch, invalid):
    pool, _, _ = system
    sandbox_dir = tmp_path / 'launcher'
    sandbox_dir.mkdir()
    sb = Sandbox(sandbox_dir)
    repo, revision = make_repo(sb)
    launcher_module = importlib.import_module('plugins.resource.worker-pool.launcher_client')
    launched = set()

    def call(sub, job, request=None, *args):
        code, result = sb.call(sub, job, *args, stdin=request)
        if code:
            raise launcher_module.LauncherError(code, result.get('error', {}).get('reason', 'error'))
        return result

    def start(self, req):
        result = call('job-start', req['job_id'], req)
        launched.add(req['job_id'])
        return result

    monkeypatch.setattr(LauncherAdapter, 'job_snapshot', lambda self, jid, req: call('job-snapshot', jid, req), raising=False)
    monkeypatch.setattr(LauncherAdapter, 'job_start', start)
    monkeypatch.setattr(LauncherAdapter, 'job_report', lambda self, jid: call('job-report', jid), raising=False)
    monkeypatch.setattr(LauncherAdapter, 'job_status', lambda self, jid: call('job-status', jid))
    monkeypatch.setattr(LauncherAdapter, 'job_log', lambda self, jid: call('job-log', jid, None, '65536'))
    monkeypatch.setattr(LauncherAdapter, 'job_stop', lambda self, jid: call('job-stop', jid))
    ledger_service = LedgerService(LedgerStore(tmp_path / 'ledger-full.sqlite'), None)
    ledger_app = create_core_app(ledger_service)
    ledger_app.state.core_tokens = TokenTable({'intake': sha256_hex('test-intake')})
    ledger = TestClient(ledger_app)

    def transport(method, url, headers, raw, timeout):
        parsed = urlsplit(url)
        response = ledger.request(method, parsed.path + (('?' + parsed.query) if parsed.query else ''),
                                  headers=headers, content=raw)
        return response.status_code, {}, response.content

    class Pool:
        def submit(self, request):
            response = pool.post('/v1/jobs', json=request)
            assert response.status_code == 202, response.text
            return response.json()
        def job(self, job_id):
            response = pool.get('/v1/jobs/' + job_id)
            assert response.status_code == 200
            return response.json()

    config = write_registry(tmp_path / 'intake-config', [simple_dest('work.request',
        schema='schemas/dest/work.request.json', handler='b12:work.request', tier='operate')])
    app = build_app(config_dir=config, dest_dir=tmp_path / 'no-dest', ext_dir=tmp_path / 'no-ext',
                    transport=transport, run_heartbeat_loop=False)
    app.state.b12 = Engine(app, store=IntakeStore(tmp_path / 'intake-full.sqlite'))
    engine = app.state.b12
    engine.ledger = LedgerClient('http://ledger:8080', token='test-intake', transport=transport)
    engine.pool = Pool()
    work.register(app)

    async def mouth(scope, receive, send):
        if scope['type'] == 'http':
            scope['ax_ident'] = Ident('local', 'commander', 'alice', 'operate')
        await app(scope, receive, send)

    client = TestClient(mouth)
    try:
        payload = {'project': 'project-a', 'title': 'Inspect', 'role': 'summary', 'body': 'Inspect source',
                   'target': 'repo', 'revision': revision, 'attachments': []}
        response = client.post('/v1/instructions', json=instruction('work.request', payload, idempotency_key='full'))
        assert response.status_code == 200, response.text
        request = response.json()['result']
        job_id, todo_id = request['job_id'], request['todo_id']
        assert ledger.get('/v1/todos/' + todo_id).json()['state'] == 'running'
        wait_for(lambda: job_id in launched)
        root = sb.hddtmp / 'research-snapshots' / job_id
        manifest = json.loads((root / 'manifest.json').read_text())
        tree = root / manifest['tree']
        assert f'{tree}:/target:ro' in sb.run_args(job_id)
        (repo / 'source.txt').write_text('later change\n')
        report = {'type': 'ax.research-report.v1', 'revision': revision, 'findings': [
            {'statement': 'Finding', 'citations': [{'path': 'missing.txt' if invalid else 'source.txt',
                                                     'line_start': 1, 'line_end': 1}]}]}
        report_path = sb.work_dir(job_id) / 'research-report.json'
        report_path.write_text(json.dumps(report))
        sb.fd.finish('ax-job-' + job_id, logs='STATUS: ready\n')
        verify_id = 'verify-' + job_id
        wait_for(lambda: verify_id in launched)
        assert f'{tree}:/target:ro' in sb.run_args(verify_id)
        verifier = Path(__file__).resolve().parents[3] / 'runtime/images/verify/python/read_verify.py'
        verified = subprocess.run(['python3', str(verifier), '--revision', revision, '--report', str(report_path),
                                   '--snapshot', str(tree)], capture_output=True, text=True)
        assert verified.returncode == (1 if invalid else 0), verified.stdout + verified.stderr
        sb.fd.finish('ax-job-' + verify_id, exit_code=verified.returncode, logs=verified.stdout)
        wait_for(lambda: pool.get('/v1/jobs/' + job_id).json()['state'] in ('ok', 'failed'))
        terminal = pool.get('/v1/jobs/' + job_id).json()
        assert terminal['state'] == ('failed' if invalid else 'ok')
        engine.event({'kind': 'job.state', 'source': {'component': 'worker-pool'},
                      'data': {'job_id': job_id, 'state': terminal['state']}})
        result = ledger.get('/v1/todos/' + todo_id + '?history=1').json()
        assert result['state'] == ('parked' if invalid else 'review')
        if invalid:
            assert result['reason'] and result['check_at']
        else:
            assert terminal['verify']['rc'] == 0 and json.loads(terminal['report']['text']) == report
        assert (tree / 'source.txt').read_text() == 'first\n'
        engine.event({'kind': 'job.state', 'source': {'component': 'worker-pool'},
                      'data': {'job_id': job_id, 'state': terminal['state']}})
        assert len(ledger.get('/v1/todos/' + todo_id + '?history=1').json()['history']) == len(result['history'])
    finally:
        client.close()
        app.state.intake.executor.shutdown()
        engine.store.close()
        ledger.close()
        ledger_service.store.close()
