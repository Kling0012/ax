"""Exercise the sandbox runner through the public adapter and a local fake ibox."""
import hashlib
import os
from pathlib import Path
import sys

import pytest

from axcommon.redact import Redactor
from core.resources.envs.sandbox.sbx_run import SandboxRun
from core.resources.envs.sandbox_policy import SandboxPolicy


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    index, pii = tmp_path / 'index', tmp_path / 'pii'
    index.write_text('fixture-salt\n')
    pii.write_text('fixture-private-value\n')
    redactor = Redactor(index_path=index, pii_list_path=pii)
    monkeypatch.setattr('core.resources.envs.adapters.cloud_sandbox.from_environment', lambda: redactor)
    policy = SandboxPolicy(caller='gate', secrets_dir=tmp_path, redactor=redactor)
    work = tmp_path / 'work'
    work.mkdir()
    ibox = tmp_path / 'ibox'
    ibox.write_text('''#!''' + sys.executable + '''
import os, shlex, shutil, subprocess, sys
from pathlib import Path
if sys.argv[1] == 'put':
    shutil.copyfile(sys.argv[2], os.environ['FAKE_RUNNER'])
elif sys.argv[1] == 'exec':
    args = shlex.split(sys.argv[2])
    if args[0] == 'python3':
        args[1] = os.environ['FAKE_RUNNER']
        sys.exit(subprocess.call(args))
    if args[:3] == ['base64', '-w0', '--']:
        import base64
        sys.stdout.buffer.write(base64.b64encode(Path(args[3]).read_bytes()))
    else:
        sys.exit(1)
else:
    sys.exit(1)
''')
    ibox.chmod(0o700)
    monkeypatch.setenv('FAKE_RUNNER', str(tmp_path / 'runner.py'))
    def run(command, artifacts=(), timeout=3):
        spec = {'job_id': 'job1', 'idempotency_key': 'job1', 'sensitivity': 'public',
                'secrets': [], 'entry': {'command': command}, 'workdir': str(work),
                'artifacts': list(artifacts)}
        context = {'spec': spec, 'environment': {'work_credentials': []},
                   'required_credentials': (), 'operations': [], 'root': str(work)}
        runner = SandboxRun(executable=str(ibox), remote_id='assigned', policy=policy,
                            resolve_job=lambda env, job: context, repo_is_public=lambda _: False,
                            timeout=timeout, cancel_grace=.1)
        return runner, runner.run(spec)
    return work, run


def test_success_and_artifact_hash(sandbox):
    work, run = sandbox
    runner, result = run([sys.executable, '-c', "from pathlib import Path; Path('out').write_text('hello'); print('done')"], ['out'])
    assert result['state'] == 'succeeded'
    assert 'done\n' in result['logs']
    artifact = runner._adapter.fetch('job1')[0]
    assert artifact.sha256 == hashlib.sha256(b'hello').hexdigest()
    assert artifact.content == b'hello'


def test_timeout_term_then_kill(sandbox):
    work, run = sandbox
    code = "import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, lambda *a: Path('term').write_text('term')); print('ready', flush=True); time.sleep(20)"
    runner, result = run([sys.executable, '-c', code], timeout=.3)
    assert result['state'] == 'failed'
    assert result['reason'] == 'timeout'
    assert (result['local_state'], result['remote_state']) == ('stopped', 'unknown')
    assert (work / 'term').read_text() == 'term'
    assert 'ready\n' in runner._adapter.logs('job1')


def test_timeout_secret_output_is_rejected(sandbox):
    _, run = sandbox
    code = "import time; print('fixture-' + 'private-value', flush=True); time.sleep(20)"
    runner, result = run([sys.executable, '-c', code], timeout=.3)
    assert result['state'] == 'failed'
    assert result['reason'] == 'timeout'
    assert runner._adapter.logs('job1') == ()


def test_outside_path_rejected_before_execution(sandbox):
    work, run = sandbox
    _, result = run([sys.executable, '-c', "from pathlib import Path; Path('executed').touch()"], ['../outside'])
    assert result['state'] == 'failed'
    assert not (work / 'executed').exists()


def test_arguments_are_not_shell_expanded(sandbox):
    work, run = sandbox
    _, result = run([sys.executable, '-c', 'import sys; print(sys.argv[1:])', '; touch BAD', '$(touch BAD2)'])
    assert result['state'] == 'succeeded'
    assert '; touch BAD' in result['logs'][0] and '$(touch BAD2)' in result['logs'][0]
    assert not (work / 'BAD').exists() and not (work / 'BAD2').exists()
