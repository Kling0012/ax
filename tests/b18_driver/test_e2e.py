import json
import threading
import time

import pytest

from axcommon.redact import Redactor
from core.resources.envs.adapters.base import AdapterError
from core.resources.envs.adapters.cloud_sandbox import CloudSandboxAdapter
from core.resources.envs.sandbox.instabox import InstaboxDriver, InstaboxError, UnsupportedOperation


@pytest.fixture
def scanner(monkeypatch):
    redactor = Redactor(pii_list=['private-person-name'], allow_email_domains=[])
    redactor.index_path = 'test-index'
    redactor.pii_list_path = 'test-pii-list'
    monkeypatch.setattr('core.resources.envs.adapters.cloud_sandbox.from_environment', lambda: redactor)


@pytest.fixture
def ibox(tmp_path, monkeypatch):
    executable = tmp_path / 'fake-ibox'
    calls = tmp_path / 'calls'
    marker = tmp_path / 'entered'
    release = tmp_path / 'release'
    executable.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time, signal, shlex
args = sys.argv[1:]
if len(args) == 3 and args[0] == 'put':
    assert pathlib.Path(args[1]).name == 'ax_sbx_run.py'
    assert args[2] == '/data/workspace/.ax/ax-sbx-run.py'
    call = ['put', args[2]]
elif len(args) == 2 and args[0] == 'exec' and args[1].startswith('python3 '):
    parts = shlex.split(args[1])
    assert parts[:2] == ['python3', '/data/workspace/.ax/ax-sbx-run.py'] and len(parts) == 3
    spec = json.loads(parts[2])
    assert spec['workdir'] == '/data/workspace' and spec['artifacts'] == []
    command = spec['command'][0]
    call = ['exec', command]
else:
    call = args
with open(os.environ['FAKE_IBOX_CALLS'], 'a') as out:
    out.write(json.dumps(call) + '\\n')
if args[0] == 'put':
    sys.exit(0)
if len(args) == 2 and args[0] == 'exec' and args[1].startswith('base64 -w0 -- '):
    print('cmVzdWx0', end='')
    sys.exit(0)
if len(args) == 2 and args[0] == 'exec' and args[1].startswith('python3 '):
    if command == 'wait':
        marker = pathlib.Path(os.environ['FAKE_IBOX_ENTERED'])
        signal.signal(signal.SIGTERM, lambda *_: marker.with_suffix('.term').touch())
        marker.touch()
        deadline = time.monotonic() + spec['timeout_seconds']
        while not pathlib.Path(os.environ['FAKE_IBOX_RELEASE']).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if time.monotonic() >= deadline:
            marker.with_suffix('.term').touch()
            print(json.dumps({'exit_code': -15, 'timed_out': True, 'stdout': '', 'stderr': '', 'artifacts': []}))
            sys.exit(0)
    print(json.dumps({'exit_code': 2 if command == 'fail' else 0, 'timed_out': False, 'stdout': '' if command == 'fail' else 'done\\n', 'stderr': '', 'artifacts': []}))
else:
    print('done')
''')
    executable.chmod(0o700)
    monkeypatch.setenv('FAKE_IBOX_CALLS', str(calls))
    monkeypatch.setenv('FAKE_IBOX_ENTERED', str(marker))
    monkeypatch.setenv('FAKE_IBOX_RELEASE', str(release))
    return executable, calls, marker, release


def spec(job='job-1', **extra):
    return {'job_id': job, 'idempotency_key': job, 'sensitivity': 'public', 'entry': {'command': 'hello'}, **extra}


def test_public_job_and_artifact(scanner, ibox):
    executable, calls, _, _ = ibox
    driver = InstaboxDriver(str(executable))
    adapter = CloudSandboxAdapter(driver=driver, remote_id='instabox')
    assert adapter.submit(spec())['state'] == 'succeeded'
    assert adapter.collect('job-1', lambda text: False).logs == ('done\n',)
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [['put', '/data/workspace/.ax/ax-sbx-run.py'], ['exec', 'hello']]
    assert adapter.lookup('job-1')['state'] == 'succeeded'
    assert driver.fetch('/data/workspace/result') == b'result'


@pytest.mark.parametrize('changes', [dict(sensitivity='private'), dict(sensitivity=None), dict(secrets=['credential']), dict(entry={'command': 'private-person-name'}), dict(entry={'command': 'test-user-10@example.com'})])
def test_sensitive_input_never_reaches_provider(scanner, ibox, changes):
    executable, calls, _, _ = ibox
    adapter = CloudSandboxAdapter(driver=InstaboxDriver(str(executable)), remote_id='instabox')
    payload = spec()
    payload.update(changes)
    with pytest.raises(AdapterError):
        adapter.submit(payload)
    assert not calls.exists()


def test_failure_and_cleanup_failure_are_isolated(scanner, ibox):
    executable, calls, _, _ = ibox
    driver = InstaboxDriver(str(executable))
    with pytest.raises(UnsupportedOperation):
        driver.create()
    with pytest.raises(UnsupportedOperation):
        driver.destroy('anything')
    assert not calls.exists()
    adapter = CloudSandboxAdapter(driver=driver, remote_id='instabox')
    assert adapter.submit(spec(entry={'command': 'fail'}))['state'] == 'failed'
    assert adapter.submit(spec('job-2'))['state'] == 'succeeded'
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [['put', '/data/workspace/.ax/ax-sbx-run.py'], ['exec', 'fail'], ['put', '/data/workspace/.ax/ax-sbx-run.py'], ['exec', 'hello']]


def test_cancel_during_execution_destroys_box(scanner, ibox):
    executable, calls, marker, release = ibox
    adapter = CloudSandboxAdapter(driver=InstaboxDriver(str(executable), cancel_grace=0.2), remote_id='instabox')
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(adapter.submit(spec(entry={'command': 'wait'}))))
    thread.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    started = time.monotonic()
    requested = adapter.cancel('job-1')
    assert requested == {'job_id': 'job-1', 'state': 'running', 'cancel_requested': True}
    assert adapter.status('job-1')['cancel_requested'] is True
    thread.join(3)
    assert time.monotonic() - started < 2
    assert not thread.is_alive()
    assert marker.with_suffix('.term').exists()
    assert outcome[0] == {'job_id': 'job-1', 'state': 'running', 'cancel_requested': True, 'local_state': 'stopped', 'remote_state': 'unknown'}
    assert adapter.status('job-1') == outcome[0]
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [['put', '/data/workspace/.ax/ax-sbx-run.py'], ['exec', 'wait']]


def test_unavailable_scanner_denies_before_ibox(monkeypatch, ibox):
    executable, calls, _, _ = ibox
    monkeypatch.setattr('core.resources.envs.adapters.cloud_sandbox.from_environment', lambda: Redactor())
    adapter = CloudSandboxAdapter(driver=InstaboxDriver(str(executable)), remote_id='instabox')
    with pytest.raises(AdapterError):
        adapter.submit(spec())
    assert not calls.exists()


def test_public_file_transfer_and_private_rejection(scanner, ibox):
    executable, calls, _, _ = ibox
    driver = InstaboxDriver(str(executable))
    with pytest.raises(Exception):
        driver.put(b'private-person-name', '/data/workspace/out', sensitivity='public')
    with pytest.raises(Exception):
        driver.put(b'hello', '/data/workspace/out', sensitivity='unknown')
    assert not calls.exists()
    driver.put(b'hello', '/data/workspace/out', sensitivity='public')
    assert json.loads(calls.read_text()) == ['exec', 'mkdir -p -- /data/workspace && printf %s aGVsbG8= | base64 -d > /data/workspace/out']


def test_timeout_fails_only_current_operation(scanner, ibox):
    executable, calls, marker, _ = ibox
    driver = InstaboxDriver(str(executable), timeout=0.3, cancel_grace=0.1)
    adapter = CloudSandboxAdapter(driver=driver, remote_id='instabox')
    result = adapter.submit(spec(entry={'command': 'wait'}))
    assert marker.exists() and marker.with_suffix('.term').exists()
    assert result['state'] == 'failed' and result['reason'] == 'timeout'
    assert adapter.status('job-1')['reason'] == 'timeout'
    other = CloudSandboxAdapter(driver=driver, remote_id='another-box')
    assert other.submit(spec('job-2'))['state'] == 'succeeded'
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [['put', '/data/workspace/.ax/ax-sbx-run.py'], ['exec', 'wait'], ['put', '/data/workspace/.ax/ax-sbx-run.py'], ['exec', 'hello']]


def test_cancelled_box_rejects_new_execution_when_status_cannot_prove_idle(scanner, ibox):
    executable, calls, marker, _ = ibox
    driver = InstaboxDriver(str(executable), cancel_grace=0.1)
    adapter = CloudSandboxAdapter(driver=driver, remote_id='instabox')
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(adapter.submit(spec(entry={'command': 'wait'}))))
    thread.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    adapter.cancel('job-1')
    thread.join(3)
    assert not thread.is_alive()
    assert outcome[0]['remote_state'] == 'unknown'
    another_adapter = CloudSandboxAdapter(driver=driver, remote_id='instabox')
    with pytest.raises(AdapterError, match='residual processes cannot be verified'):
        another_adapter.submit(spec('job-2'))
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [['put', '/data/workspace/.ax/ax-sbx-run.py'], ['exec', 'wait'], ['status']]
