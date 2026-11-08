"""Offline path J checks through the public adapter with a fake ibox process."""
import pytest

from axcommon.redact import Redactor
from core.resources.envs.adapters.base import AdapterError
from core.resources.envs.sandbox.composition import bind_adapter
from core.resources.envs.sandbox.instabox import InstaboxDriver
from core.resources.envs.sandbox_policy import SandboxPolicy


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    index = tmp_path / 'index'
    pii = tmp_path / 'pii'
    index.write_text('fixture-salt\n')
    pii.write_text('fixture-private-value\n')
    redactor = Redactor(index_path=index, pii_list_path=pii)
    monkeypatch.setattr('core.resources.envs.adapters.cloud_sandbox.from_environment', lambda: redactor)
    policy = SandboxPolicy(caller='gate', secrets_dir=tmp_path, redactor=redactor)
    calls = tmp_path / 'calls'
    executable = tmp_path / 'ibox'
    executable.write_text('#!/usr/bin/env python3\nimport json, pathlib, shlex, sys\n'
                          'if sys.argv[1] == "put": sys.exit(0)\n'
                          f'pathlib.Path({str(calls)!r}).write_text("called")\n'
                          'command = json.loads(shlex.split(sys.argv[2])[-1])["command"][0]\n'
                          'print(json.dumps({"exit_code": 0, "timed_out": False, "stdout": "fixture-private-value" if command == "secret" else "safe\\n", "stderr": "", "artifacts": []}))\n')
    executable.chmod(0o700)
    spec = {'job_id': 'job1', 'idempotency_key': 'job1', 'sensitivity': 'public',
            'secrets': [], 'entry': {'command': 'hello'}}
    context = {'spec': spec, 'environment': {'work_credentials': []},
               'required_credentials': (), 'operations': [{'name': 'shell', 'args': {'command': 'hello'}}],
               'root': '/workspace/ax'}
    def resolve(env, job):
        if env != 'assigned' or job != 'job1':
            raise KeyError('unassigned')
        return context
    def adapter(env='assigned'):
        return bind_adapter(InstaboxDriver(str(executable)), remote_id=env, state_store=None,
                            policy=policy, resolve_job=resolve, repo_is_public=lambda _: False)
    return adapter, spec, context, calls


def test_assigned_job_runs(sandbox):
    adapter, spec, _, calls = sandbox
    run = adapter()
    assert run.submit(spec)['state'] == 'succeeded'
    assert run.logs('job1') == ('safe\n',)
    assert calls.exists()


def test_unassigned_box_is_denied(sandbox):
    adapter, spec, _, calls = sandbox
    with pytest.raises(AdapterError):
        adapter('unassigned').submit(spec)
    assert not calls.exists()


def test_secret_in_provider_output_is_not_returned(sandbox):
    adapter, spec, context, _ = sandbox
    spec['entry']['command'] = 'secret'
    context['operations'][0]['args']['command'] = 'secret'
    run = adapter()
    assert run.submit(spec)['state'] == 'failed'
    assert run.logs('job1') == ()
    assert 'fixture-private-value' not in repr(run.collect('job1', lambda _: False))


def test_persistent_box_assignment_and_cancel_quarantine(tmp_path, monkeypatch):
    import subprocess
    import sys
    import threading
    import time

    from core.resources.envs.sandbox.sqlite_gate import SQLiteGate

    index, pii = tmp_path / 'index', tmp_path / 'pii'
    index.write_text('fixture-salt\n')
    pii.write_text('fixture-private-value\n')
    redactor = Redactor(index_path=index, pii_list_path=pii)
    monkeypatch.setattr('core.resources.envs.adapters.cloud_sandbox.from_environment', lambda: redactor)
    policy = SandboxPolicy(caller='gate', secrets_dir=tmp_path, redactor=redactor)
    marker = tmp_path / 'started'
    executable = tmp_path / 'ibox'
    executable.write_text('#!/usr/bin/env python3\nimport json, pathlib, shlex, sys, time\n'
                          'if sys.argv[1] == "put": sys.exit(0)\n'
                          f'pathlib.Path({str(marker)!r}).write_text("called")\n'
                          'command = json.loads(shlex.split(sys.argv[2])[-1])["command"][0]\n'
                          'time.sleep(30) if command == "wait" else print(json.dumps({"exit_code":0,"timed_out":False,"stdout":"safe\\n","stderr":"","artifacts":[]}))\n')
    executable.chmod(0o700)
    gate = SQLiteGate(tmp_path / 'gate.db', auth_digest='0' * 64, providers={})
    spec = {'job_id': 'job1', 'idempotency_key': 'job1', 'sensitivity': 'public',
            'secrets': [], 'entry': {'command': 'wait'}}
    def resolve(env, job):
        return {'spec': spec, 'environment': {'work_credentials': []},
                'required_credentials': (), 'operations': [{'name': 'shell', 'args': {'command': spec['entry']['command']}}],
                'root': '/workspace/ax'}
    def adapter(box):
        return bind_adapter(InstaboxDriver(str(executable)), remote_id=box, state_store=gate,
                            box_bindings=gate, policy=policy, resolve_job=resolve,
                            repo_is_public=lambda _: False)
    with pytest.raises(AdapterError):
        adapter('unassigned').submit(spec)
    assert not marker.exists()
    gate.bind_box('assigned', 'job1')
    run = adapter('assigned')
    result = []
    thread = threading.Thread(target=lambda: result.append(run.submit(spec)))
    thread.start()
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert marker.exists()
    run.cancel('job1')
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result[0]['local_state'] == 'stopped'
    assert result[0]['remote_state'] == 'unknown'
    assert gate.box_state('assigned') == {'local_state': 'stopped', 'remote_state': 'unknown'}
    # A fresh interpreter exercises the persisted state without the in-process guard.
    child = r'''
import sys
from axcommon.redact import Redactor
from core.resources.envs.adapters.base import AdapterError
from core.resources.envs.sandbox.composition import bind_adapter
from core.resources.envs.sandbox.instabox import InstaboxDriver
from core.resources.envs.sandbox.sqlite_gate import SQLiteGate
from core.resources.envs.sandbox_policy import SandboxPolicy
import core.resources.envs.adapters.cloud_sandbox as cloud
from pathlib import Path
root = Path(sys.argv[1])
redactor = Redactor(index_path=root / 'index', pii_list_path=root / 'pii')
cloud.from_environment = lambda: redactor
policy = SandboxPolicy(caller='gate', secrets_dir=root, redactor=redactor)
gate = SQLiteGate(root / 'gate.db', auth_digest='0' * 64, providers={})
spec = {'job_id': 'job1', 'idempotency_key': 'job1', 'sensitivity': 'public', 'secrets': [], 'entry': {'command': 'hello'}}
def resolve(env, job):
    return {'spec': spec, 'environment': {'work_credentials': []}, 'required_credentials': (), 'operations': [{'name': 'shell', 'args': {'command': 'hello'}}], 'root': '/workspace/ax'}
def adapter():
    return bind_adapter(InstaboxDriver(str(root / 'ibox')), remote_id='assigned', state_store=gate, box_bindings=gate, policy=policy, resolve_job=resolve, repo_is_public=lambda _: False)
assert gate.box_assigned('assigned', 'job1')
assert gate.box_state('assigned') == {'local_state': 'stopped', 'remote_state': 'unknown'}
try:
    adapter().submit(spec)
except AdapterError:
    pass
else:
    raise AssertionError('quarantined box allowed')
gate.clear_remote_unknown('assigned')
assert adapter().submit(spec)['state'] == 'succeeded'
'''
    subprocess.run([sys.executable, '-c', child, str(tmp_path)], check=True, timeout=20)


def test_resources_assembly_requires_trusted_box_assignment(tmp_path, monkeypatch):
    from core.resources.app import Config, build
    from core.resources.envs.sandbox.admission import PolicyGate
    from core.resources.envs.sandbox.sqlite_gate import SQLiteGate
    index, pii = tmp_path / 'assembly-index', tmp_path / 'assembly-pii'
    index.write_text('fixture-salt\n')
    pii.write_text('fixture-private-value\n')
    redactor = Redactor(index_path=index, pii_list_path=pii)
    monkeypatch.setattr('core.resources.envs.adapters.cloud_sandbox.from_environment', lambda: redactor)
    policy = SandboxPolicy(caller='gate', secrets_dir=tmp_path, redactor=redactor)
    executable = tmp_path / 'ibox'
    executable.write_text('#!/usr/bin/env python3\nimport json, sys\nif sys.argv[1] == "put": sys.exit(0)\nprint(json.dumps({"exit_code":0,"timed_out":False,"stdout":"safe","stderr":"","artifacts":[]}))\n')
    executable.chmod(0o700)
    gate = SQLiteGate(tmp_path / 'gate.sqlite', auth_digest='0' * 64, providers={})
    spec = {'job_id': 'job1', 'idempotency_key': 'job1', 'sensitivity': 'public',
            'secrets': [], 'entry': {'command': 'hello'}}
    def resolve(env, job):
        return {'spec': spec, 'environment': {'work_credentials': []}, 'required_credentials': (),
                'operations': [{'name': 'shell', 'args': {'command': 'hello'}}], 'root': '/workspace/ax'}
    wrapped = PolicyGate(gate, policy=policy, resolve_job=resolve, repo_is_public=lambda _: False)
    # Exercise assignment enforcement independently of the resources startup guard.
    from core.resources.envs.sandbox.composition import bind_adapter
    from core.resources.envs.sandbox.sbx_run import SandboxRun
    dependencies = dict(remote_id='assigned', state_store=gate, box_bindings=gate,
                        policy=policy, resolve_job=resolve, repo_is_public=wrapped.repo_is_public)
    adapter = bind_adapter(InstaboxDriver(str(executable)), **dependencies)
    runner = SandboxRun(executable=str(executable), **dependencies)
    with pytest.raises(AdapterError):
        adapter.submit(spec)
    with pytest.raises(AdapterError):
        runner.run(spec)
    gate.bind_box('assigned', 'job1')
    assert adapter.submit(spec)['state'] == 'succeeded'
    assert runner.run(spec)['state'] == 'succeeded'
