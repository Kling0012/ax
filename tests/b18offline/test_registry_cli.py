"""Exercise the published schema CLI with the B18 example registry."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'config' / 'environments.sandbox.example.yaml'


def validate(entry, tmp_path):
    target = tmp_path / 'environment.yaml'
    target.write_text(yaml.safe_dump(entry), encoding='utf-8')
    return subprocess.run(
        [sys.executable, '-m', 'axcommon.schemas', '--validate', 'environment', str(target)],
        cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)},
        capture_output=True, text=True, check=False,
    )


def test_example_rows_pass_cli_and_forbidden_free_command_is_rejected(tmp_path):
    rows = yaml.safe_load(EXAMPLE.read_text(encoding='utf-8'))['environments']
    assert {row['id'] for row in rows} == {'box', 'instabox'}
    for row in rows:
        assert validate(row, tmp_path).returncode == 0
        for change in ({'data_classes': ['private']}, {'secrets_allowed': True},
                       {'lifetime': 'permanent'}, {'adapter': 'ssh-host'},
                       {'cost': {'kind': 'metered'}}, {'max_concurrent': 2}):
            assert validate({**row, **change}, tmp_path).returncode != 0


def test_registry_and_cli_enforce_credential_references_and_free_command(tmp_path):
    from core.resources.envs.registry.loader import load_environment_file

    document = yaml.safe_load(EXAMPLE.read_text(encoding='utf-8'))
    path = tmp_path / 'registry.yaml'

    def check(row, accepted):
        path.write_text(yaml.safe_dump({'contract_supported': '1.0', 'environments': [row]}), encoding='utf-8')
        result = load_environment_file(path)
        assert bool(result.environments) is accepted
        assert (validate(row, tmp_path).returncode == 0) is accepted

    for row in document['environments']:
        check(row, True)
        check({**row, 'model_family': 'glm'}, True)
        check({**row, 'model_family': ['glm']}, True)
        for field in ('conn_credential', 'gate_credential'):
            check({**row, field: {**row[field], 'value': 'fake-secret'}}, False)
        check({**row, 'work_credentials': [{'slot': 'identity', 'vars': {'TOKEN': 'fake-secret'}}]}, False)
        check({**row, 'work_credentials': [{'slot': 'identity', 'value': 'fake-secret'}]}, False)
        for change in ({'data_classes': ['private']}, {'secrets_allowed': True},
                       {'lifetime': 'permanent'}, {'adapter': 'ssh-host'}):
            check({**row, **change}, False)


def test_policy_public_interface_before_sandbox_transport(tmp_path):
    from axcommon.redact import Redactor
    from core.resources.box.protocol import BoxError
    from core.resources.envs.sandbox_policy import SandboxPolicy

    index = tmp_path / 'index'
    index.write_text('fixture-salt\n', encoding='utf-8')
    pii = tmp_path / 'pii'
    pii.write_text('fixture-private-person\n', encoding='utf-8')
    policy = SandboxPolicy(caller='adapter', secrets_dir=tmp_path,
                           redactor=Redactor(index_path=index, pii_list_path=pii))
    env = {'work_credentials': [{'slot': 'identity', 'place': '/workspace/identity'}]}
    sent = []

    def submit(spec, tool='shell', args=None):
        policy.check_job(env, spec, required_credentials=('identity',),
                         repo_is_public=lambda repo: repo == 'example/public')
        policy.authorize_call(tool, args or {'command': 'true'}, path='J', root='/workspace/ax')
        sent.append(tool)

    good = {'sensitivity': 'public', 'secrets': ['identity'],
            'inputs': [{'ref': 'git:example/public@abc'}]}
    submit(good)
    assert sent == ['shell']
    cases = [({**good, 'secrets': []}, 'shell', 'credential_missing'),
             ({**good, 'secrets': [{'value': 'fixture-secret'}]}, 'shell', 'credential_invalid'),
             ({**good, 'inputs': [{'ref': 'git:example/private@abc'}]}, 'shell', 'repo_not_public'),
             ({**good, 'sensitivity': 'private'}, 'shell', 'sensitivity_denied'),
             (good, 'unknown_operation', 'tool_denied')]
    for spec, tool, code in cases:
        with pytest.raises(BoxError) as error:
            submit(spec, tool)
        assert error.value.code == code
    assert sent == ['shell']
    with pytest.raises(BoxError):
        submit(good, 'computer_act', {'action': 'type_from_file'})
    with pytest.raises(BoxError):
        submit(good, 'shell', {'command': 'fixture-private-person'})
    assert sent == ['shell']


def test_policy_table_schema_cli_and_hard_denials(tmp_path):
    from axcommon.redact import Redactor
    from core.resources.envs.sandbox_policy import SandboxPolicy

    source = ROOT / 'config' / 'sandbox-tools.yaml'
    table = yaml.safe_load(source.read_text(encoding='utf-8'))
    path = tmp_path / 'tools.yaml'
    path.write_text(yaml.safe_dump(table), encoding='utf-8')
    command = [sys.executable, '-m', 'axcommon.schemas', '--validate', 'sandbox-tools', str(path)]
    run = lambda: subprocess.run(command, cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)},
                                 capture_output=True, text=True, check=False)
    assert run().returncode == 0
    table['tools']['machine_delete'] = {'box': 'box_delete', 'instabox': None, 'capability': None, 'paths': ['J']}
    path.write_text(yaml.safe_dump(table), encoding='utf-8')
    with pytest.raises(ValueError):
        SandboxPolicy(caller='adapter', secrets_dir=tmp_path, redactor=Redactor(), tools_path=path)
    table['tools']['machine_delete']['paths'] = 'J'
    path.write_text(yaml.safe_dump(table), encoding='utf-8')
    assert run().returncode != 0
