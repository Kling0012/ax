"""One-shot execution against a shell-only fake ibox, without network access."""
import threading
import time

import pytest

from axcommon.redact import Redactor
from core.resources.envs.adapters.base import AdapterError
from core.resources.envs.sandbox.sbx_run import SandboxRun
from core.resources.envs.sandbox_policy import SandboxPolicy


@pytest.fixture
def setup_run(tmp_path, monkeypatch):
    index, pii = tmp_path / 'index', tmp_path / 'pii'
    index.write_text('fixture-salt\n')
    pii.write_text('fixture-private-value\n')
    redactor = Redactor(index_path=index, pii_list_path=pii)
    monkeypatch.setattr('core.resources.envs.adapters.cloud_sandbox.from_environment', lambda: redactor)
    policy = SandboxPolicy(caller='gate', secrets_dir=tmp_path, redactor=redactor)
    calls, term = tmp_path / 'calls', tmp_path / 'term'
    ibox = tmp_path / 'ibox'
    ibox.write_text('#!/usr/bin/env python3\n'
                    'import json, pathlib, shlex, signal, sys, time\n'
                    'if sys.argv[1] == "put": sys.exit(0)\n'
                    f'pathlib.Path({str(calls)!r}).write_text("called")\n'
                    'command = json.loads(shlex.split(sys.argv[2])[-1])["command"][0]\n'
                    f'if command == "wait":\n signal.signal(signal.SIGTERM, lambda *a: pathlib.Path({str(term)!r}).write_text("term")); time.sleep(20)\n'
                    'print(json.dumps({"exit_code": 0, "timed_out": False, "stdout": "fixture-private-value" if command == "secret" else "safe\\n", "stderr": "", "artifacts": []}))\n')
    ibox.chmod(0o700)
    def make(command='hello', assigned=True, timeout=2):
        spec = {'job_id': 'job1', 'idempotency_key': 'job1', 'sensitivity': 'public',
                'secrets': [], 'entry': {'command': command}}
        context = {'spec': spec, 'environment': {'work_credentials': []},
                   'required_credentials': (), 'operations': [{'name': 'shell', 'args': {'command': command}}],
                   'root': '/workspace/ax'}
        def resolve(env, job):
            if not assigned or env != 'assigned' or job != 'job1':
                raise KeyError('unassigned')
            return context
        runner = SandboxRun(executable=str(ibox), remote_id='assigned', policy=policy,
                            resolve_job=resolve, repo_is_public=lambda _: False,
                            timeout=timeout, cancel_grace=0.1)
        return runner, spec
    return make, calls, term


def test_success(setup_run):
    make, calls, _ = setup_run
    runner, spec = make()
    result = runner.run(spec)
    assert result['state'] == 'succeeded' and result['logs'] == ('safe\n',)
    assert calls.exists()


def test_timeout_sends_term_then_kill(setup_run):
    make, _, term = setup_run
    runner, spec = make('wait', timeout=0.2)
    start = time.monotonic()
    result = runner.run(spec)
    assert term.read_text().strip() == 'term'
    assert time.monotonic() - start < 8
    assert result['reason'] == 'timeout'
    assert (result['local_state'], result['remote_state']) == ('stopped', 'unknown')


def test_unassigned_never_calls_ibox(setup_run):
    make, calls, _ = setup_run
    runner, spec = make(assigned=False)
    with pytest.raises(AdapterError):
        runner.run(spec)
    assert not calls.exists()


def test_secret_output_is_rejected(setup_run):
    make, _, _ = setup_run
    runner, spec = make('secret')
    result = runner.run(spec)
    assert result['state'] == 'failed'
    assert 'fixture-private-value' not in repr(result)


def test_cancel_stops_local_and_marks_remote_unknown(setup_run):
    make, calls, term = setup_run
    runner, spec = make('wait')
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.run(spec)))
    thread.start()
    deadline = time.monotonic() + 2
    while not calls.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls.exists()
    runner.cancel('job1')
    thread.join(3)
    assert not thread.is_alive()
    assert term.exists()
    assert (results[0]['local_state'], results[0]['remote_state']) == ('stopped', 'unknown')
