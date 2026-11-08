"""Public intake through pool and launcher with a fake model and Docker."""
import importlib
import json
import subprocess
from pathlib import Path

import yaml
import pytest

from tests.b5.helpers import client_for, instruction, make_app, simple_dest
from tests.b6a.test_dispatch_e2e import LauncherAdapter, system, wait_for
from tests.b6b.helpers import Sandbox
from tests.b6b.test_research_snapshot_e2e import make_repo
from tests.fakes.b12.work_core.peers import setup


@pytest.mark.parametrize("case", ["valid", "invalid", "tampered"])
def test_work_request_snapshot_and_independent_verification(system, tmp_path, monkeypatch, case):
    invalid = case == "invalid"
    pool, _, _ = system
    sandbox_dir = tmp_path / 'launcher'
    sandbox_dir.mkdir()
    sb = Sandbox(sandbox_dir)
    repo, revision = make_repo(sb)
    material = sb.tmp / 'docs'
    material.mkdir()
    (material / 'guide.txt').write_text('reference\n')
    registry_path = sb.ax_home / 'config/ro-mounts.yaml'
    registry = yaml.safe_load(registry_path.read_text())
    registry.append({'id': 'docs', 'project': 'project-a', 'host_path': str(material),
                     'container_path': '/docs', 'sensitivity': 'private', 'files': ['guide.txt']})
    registry_path.write_text(yaml.safe_dump(registry))
    launcher_module = importlib.import_module('plugins.resource.worker-pool.launcher_client')

    def call(sub, job, request=None, *args):
        code, result = sb.call(sub, job, *args, stdin=request)
        if code:
            raise launcher_module.LauncherError(code, result.get('error', {}).get('reason', 'error'))
        return result

    monkeypatch.setattr(LauncherAdapter, 'job_snapshot', lambda self, jid, req: call('job-snapshot', jid, req), raising=False)
    launched = set()
    def start_job(self, req):
        result = call('job-start', req['job_id'], req)
        launched.add(req['job_id'])
        return result
    monkeypatch.setattr(LauncherAdapter, 'job_start', start_job)
    monkeypatch.setattr(LauncherAdapter, 'job_report', lambda self, jid: call('job-report', jid), raising=False)
    monkeypatch.setattr(LauncherAdapter, 'job_status', lambda self, jid: call('job-status', jid))
    monkeypatch.setattr(LauncherAdapter, 'job_log', lambda self, jid: call('job-log', jid, None, '65536'))
    monkeypatch.setattr(LauncherAdapter, 'job_stop', lambda self, jid: call('job-stop', jid))
    fake, peers, _, _ = setup()

    class Pool:
        def submit(self, job):
            response = pool.post('/v1/jobs', json=job)
            assert response.status_code == 202, response.text
            return response.json()

    fake.state.b12.pool = Pool()
    entry = simple_dest('work.request', schema='schemas/dest/work.request.json', handler='b12:work.request', tier='operate')
    app = make_app(tmp_path / 'intake', peers, [entry])
    app.state.b12 = fake.state.b12
    app.state.intake.sink = None
    from core.intake.dest.b12 import work
    app.state.intake.dest_handlers['work.request'] = work.handle
    payload = {'project': 'project-a', 'title': 'Read', 'role': 'summary', 'body': 'Read sources',
               'target': 'repo', 'revision': revision, 'attachments': ['docs']}
    with client_for(app) as client:
        bad = client.post('/v1/instructions', json=instruction('work.request', {'project': 'project-a', 'title': 'Read', 'role': 'summary'}, idempotency_key='bad'))
        assert bad.status_code == 422
        response = client.post('/v1/instructions', json=instruction('work.request', payload, idempotency_key='read-1'))
        assert response.status_code == 200, response.text
    job_id = response.json()['result']['job_id']
    def container_exists(name):
        try:
            return sb.fd.container(name) is not None
        except (ValueError, OSError):
            return False
    wait_for(lambda: job_id in launched and container_exists('ax-job-' + job_id))
    root = sb.hddtmp / 'research-snapshots' / job_id
    manifest = json.loads((root / 'manifest.json').read_text())
    tree = root / manifest['tree']
    (repo / 'source.txt').write_text('changed\n')
    (material / 'guide.txt').write_text('changed\n')
    report = {'type': 'ax.research-report.v1', 'revision': revision, 'findings': [
        {'statement': 'Finding', 'citations': [{'path': 'source.txt', 'line_start': 1, 'line_end': 1},
         {'path': '.ax-attachments/docs/guide.txt', 'line_start': 1, 'line_end': 1}]}]}
    if invalid:
        report['findings'][0]['citations'][0]['path'] = 'missing.txt'
    report_path = sb.work_dir(job_id) / 'research-report.json'
    report_path.write_text(json.dumps(report))
    worker_args = sb.run_args(job_id)
    sb.fd.finish('ax-job-' + job_id, logs='STATUS: ready\n')
    verify_id = 'verify-' + job_id
    wait_for(lambda: verify_id in launched and container_exists('ax-job-' + verify_id))
    assert f'{tree}:/target:ro' in worker_args
    assert f'{tree}:/target:ro' in sb.run_args(verify_id)
    verifier = Path(__file__).resolve().parents[2] / 'runtime/images/verify/python/read_verify.py'
    verified = subprocess.run(['python3', str(verifier), '--revision', revision, '--report', str(report_path), '--snapshot', str(tree)], capture_output=True, text=True)
    assert verified.returncode == (1 if invalid else 0), verified.stdout + verified.stderr
    if case == 'tampered':
        report_path.write_text('{}')
    sb.fd.finish('ax-job-' + verify_id, exit_code=verified.returncode, logs=verified.stdout)
    expected = 'failed' if case != 'valid' else 'ok'
    wait_for(lambda: pool.get('/v1/jobs/' + job_id).json()['state'] == expected)
    result = pool.get('/v1/jobs/' + job_id).json()
    assert result['verify']['rc'] == (1 if invalid else 0)
    if case == 'valid':
        assert result['report']['status'] == 'ready'
        assert json.loads(result['report']['text']) == report
    assert (tree / 'source.txt').read_text() == 'first\n'
    assert (tree / '.ax-attachments/docs/guide.txt').read_text() == 'reference\n'
