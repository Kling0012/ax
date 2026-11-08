"""Exercise the CLI and restricted remote dispatcher with fake SSH commands."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / 'host/env-module-min/releases/b6/lib/ax_launcher.py'


@pytest.mark.parametrize('scenario', ['ok', 'sudo_failure', 'unreachable', 'no_transfer_evidence', 'match_failure', 'missing_key', 'empty_options', 'command_option', 'bad_connection'])
def test_cli_probe_with_fake_ssh(tmp_path, scenario):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    log = tmp_path / 'commands.log'
    def fake(name, body):
        path = bindir / name
        path.write_text('#!' + sys.executable + '\n' + body, encoding='utf-8')
        path.chmod(0o755)
    fake('ssh', '''import json, os, subprocess, sys
with open(os.environ['FAKE_LOG'], 'a') as log: log.write(json.dumps(['ssh', *sys.argv[1:]]) + '\\n')
if os.environ['SCENARIO'] == 'unreachable': sys.exit(255)
assert sys.argv[-1] == 'probe'
env = dict(os.environ, SSH_ORIGINAL_COMMAND='probe', SSH_CONNECTION=('bad' if os.environ['SCENARIO'] == 'bad_connection' else '127.0.0.1 43210 127.0.0.1 22'))
r = subprocess.run([sys.executable, os.environ['LAUNCHER'], '--caller', 'env'], env=env)
sys.exit(r.returncode)
''')
    fake('sudo', '''import json, os, sys
with open(os.environ['FAKE_LOG'], 'a') as log: log.write(json.dumps(['sudo', *sys.argv[1:]]) + '\\n')
assert sys.argv[1:4] == ['-n', 'sshd', '-T']
if os.environ['SCENARIO'] == 'sudo_failure': sys.exit(1)
print('passwordauthentication no')
print('acceptenv LANG')
print('acceptenv LC_*')
if len(sys.argv) > 4:
    assert sys.argv[4:] == ['-C', 'user=fake-user,host=localhost,addr=127.0.0.1']
    if os.environ['SCENARIO'] == 'match_failure': sys.exit(1)
    print('disableforwarding no')
    print('allowtcpforwarding yes')
    print('authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2')
''')
    fake('df', '''import json, os, sys
with open(os.environ['FAKE_LOG'], 'a') as log: log.write(json.dumps(['df', *sys.argv[1:]]) + '\\n')
assert sys.argv[1:3] == ['-Pk', '--']
print('Filesystem 1024-blocks Used Available Capacity Mounted on')
print('/dev/fake 100 10 90 10% /')
''')
    fake('sshd', 'pass\n')
    fake('id', "import sys\nassert sys.argv[1:] == ['-un']\nprint('fake-user')\n")
    (tmp_path / '.ssh').mkdir()
    options = {'no_transfer_evidence': 'no-pty', 'empty_options': '', 'command_option': 'command=\"/bin/true\"'}.get(scenario, 'restrict')
    key_comment = 'different' if scenario == 'missing_key' else 'ax-probe'
    (tmp_path / '.ssh/authorized_keys').write_text(f'{options + ' ' if options else ''}ssh-ed25519 SECRETKEYBODY {key_comment}\n')
    doc = yaml.safe_load((ROOT / 'config/environments.example.yaml').read_text())
    row = doc['environments'][0]
    row['connect'] = {'kind': 'ssh', 'host': 'fake-host', 'user': 'fake-user', 'port': 22, 'key_comment': 'ax-probe'}
    doc['environments'] = [row]
    config = tmp_path / 'envs.yaml'
    config.write_text(yaml.safe_dump(doc))
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'],
               PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / 'host/lib')]),
               AX_ENVIRONMENTS_FILE=str(config), AX_ENV_HOME=str(tmp_path), HOME=str(tmp_path),
               FAKE_LOG=str(log), SCENARIO=scenario, LAUNCHER=str(LAUNCHER))
    result = subprocess.run([sys.executable, '-c', 'import ax_cli; raise SystemExit(ax_cli.main())',
                             'env', 'probe', row['id'], '--json'], env=env, capture_output=True, text=True)
    assert result.stderr == ''
    output = json.loads(result.stdout)
    checks = output['data']
    assert set(checks) == {'ssh', 'transfer', 'commands', 'disk', 'sudo'}
    assert result.returncode == (0 if scenario in ('ok', 'command_option') else 1)
    if scenario in ('ok', 'command_option'):
        assert set(checks.values()) == {'ok'}
    elif scenario == 'sudo_failure':
        assert checks['sudo'] == checks['transfer'] == 'unknown'
    elif scenario == 'unreachable':
        assert checks['ssh'] == 'ng'
        assert all(checks[k] == 'unknown' for k in checks if k != 'ssh')
    elif scenario == 'empty_options':
        assert checks['transfer'] == 'ng'
    else:
        assert checks['transfer'] == 'unknown'
    assert 'SECRETKEYBODY' not in result.stdout + result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0][-1] == 'probe'
    assert all(call[0] in {'ssh', 'sudo', 'df', 'id'} for call in calls)
    assert all(call[:4] == ['sudo', '-n', 'sshd', '-T'] for call in calls if call[0] == 'sudo')
    assert all(call[1:3] == ['-Pk', '--'] for call in calls if call[0] == 'df')
