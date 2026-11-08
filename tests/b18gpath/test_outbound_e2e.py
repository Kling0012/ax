"""G-path HTTP calls through a guarded fake ibox transport."""
import subprocess

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from axcommon.redact import Redactor
from core.resources.envs.sandbox.mcp import SandboxMCP, build_mcp_routes
from core.resources.envs.sandbox.outbound import guard_outbound
from core.resources.envs.sandbox_policy import SandboxPolicy


@pytest.mark.parametrize('case', ['public', 'unknown', 'private', 'secret', 'classifier_error', 'scanner_error'])
def test_gate_prevents_unsafe_provider_calls(tmp_path, caplog, case):
    index, pii = tmp_path / 'index', tmp_path / 'pii'
    index.write_text('fixture-salt\n')
    pii.write_text('fixture-private-value\n')
    policy = SandboxPolicy(caller='gate', secrets_dir=tmp_path,
                           redactor=Redactor(index_path=index, pii_list_path=pii))
    calls = tmp_path / 'calls'
    ibox = tmp_path / 'ibox'
    ibox.write_text('#!/usr/bin/env python3\nimport pathlib\n'
                    f'pathlib.Path({str(calls)!r}).write_text("called")\n'
                    'print("safe")\n')
    ibox.chmod(0o700)
    context = {'environment': {'work_credentials': []}, 'required_credentials': (),
               'root': '/workspace/ax', 'spec': {'sensitivity': 'public', 'secrets': []},
               'operations': [{'name': 'shell', 'args': {'command': 'true'}}]}
    class Gate:
        def token_context(self, env, token):
            return {'job_id': 'job1', 'capabilities': ['linux-sandbox']}
    def classify(env, mapping, args):
        if case == 'classifier_error':
            raise RuntimeError('private details')
        return {'unknown': None, 'private': 'private'}.get(case, 'public')
    def scan(args):
        if case == 'scanner_error':
            raise RuntimeError('private details')
        return policy.scan_outbound(args)
    def invoke(env, mapping, args):
        return subprocess.check_output([str(ibox), 'exec', args['command']], text=True)
    events = []
    wrapped = guard_outbound(invoke, classify=classify, scan_outbound=scan,
                             record_denial=lambda env, reason: events.append((env, reason)))
    service = SandboxMCP(gate=Gate(), policy=policy, resolve_job=lambda env, job: context,
                         repo_is_public=lambda repo: True, implementations={'box': {'shell'}}, invoke=wrapped)
    command = 'fixture-private-value' if case == 'secret' else 'true'
    with TestClient(Starlette(routes=build_mcp_routes(service))) as client:
        result = client.post('/mcp/box', headers={'Authorization': 'Bearer fake'},
                             json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                   'params': {'name': 'shell', 'arguments': {'command': command}}}).json()
    assert calls.exists() == (case == 'public')
    assert ('result' in result) == (case == 'public')
    if case in ('unknown', 'private', 'classifier_error'):
        assert events == [('box', 'repo_not_public')]
    elif case == 'secret':
        assert 'reason=scan_blocked' in caplog.text
        assert 'fixture-private-value' not in caplog.text
    elif case == 'scanner_error':
        assert events == [('box', 'scan_blocked')]
    elif case == 'public':
        assert events == []
