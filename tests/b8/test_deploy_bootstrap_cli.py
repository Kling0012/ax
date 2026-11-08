"""Exercise bootstrap through the public shell commands with isolated fakes."""
import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SHA = 'a' * 40


def test_bootstrap_missing_images_failure_retry_and_idempotence(tmp_path):
    home = tmp_path / 'axhome'
    home.mkdir()
    (home / 'uv.lock').write_text('[[package]]\nname="jsonschema"\nversion="4.26.0"\n')
    profile = yaml.safe_load((ROOT / 'profile/example.yaml').read_text())
    profile.update(secrets_dir=str(tmp_path / 'secrets'), tmp_dir=str(tmp_path / 'tmp'),
                   ax_state_dir=str(tmp_path / 'state'))
    path = tmp_path / 'profile.yaml'
    path.write_text(yaml.safe_dump(profile))
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    git = bin_dir / 'git'
    git.write_text('''#!/usr/bin/python3
import sys
a = sys.argv
if 'rev-parse' in a: print('a' * 40)
elif 'tag' in a: print('accepted/' + 'a' * 12)
''')
    git.chmod(0o755)
    sudo = bin_dir / 'sudo-fake'
    sudo.write_text('''#!/usr/bin/python3
import json, os, pathlib, sys
args = sys.argv[1:]
base = pathlib.Path(os.environ['FAKE_ROOT'])
with (base / 'calls').open('a') as f: f.write(json.dumps(args) + '\\n')
if 'config' in args and '--environment' in args: pass
elif 'config' in args:
 import yaml
 networks = {}
 for name, subnet, gateway in (('ax-core', '192.0.2.0/25', '192.0.2.1'),
                               ('ax-work', '198.51.100.0/25', '198.51.100.1'),
                               ('ax-publish-review', '192.0.2.128/26', '192.0.2.129'),
                               ('ax-job', '203.0.113.0/25', '203.0.113.1')):
  networks[name] = {'name': 'ax_' + name, 'ipam': {'config': [{'subnet': subnet, 'gateway': gateway}]}}
 for i, arg in enumerate(args[:-1]):
  if arg == '-f' and args[i + 1].endswith('.compose.yaml') and '/ax-ipam-' in args[i + 1]:
   with open(args[i + 1]) as stream: overlay = yaml.safe_load(stream) or {}
   for name, value in overlay.get('networks', {}).items():
    networks[name]['ipam']['config'][0].update(value['ipam']['config'][0])
 print(json.dumps({'name': 'ax', 'networks': networks, 'services': {'img-runtime': {'image': 'ax/runtime:' + 'a' * 12, 'build': {'context': '.'}, 'profiles': ['build']}, 'timeline': {'image': 'ax/core:' + 'a' * 12}}}))
elif args[:3] == ['docker', 'network', 'ls']:
 pass
elif args[:3] == ['docker', 'image', 'inspect']:
 sys.exit(0 if (base / 'images').exists() else 1)
elif 'build' in args and '--build-arg' in args:
 if os.environ.get('FAKE_FAIL') == '1': sys.exit(1)
 (base / 'images').touch()
''')
    sudo.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('AX_', 'FAKE_'))}
    env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", AX_PROFILE=str(path), AX_HOME=str(home),
               AX_HOSTWATCH_SUDO=str(sudo), FAKE_ROOT=str(tmp_path), PYTHONDONTWRITEBYTECODE='1')
    def run(script, *args):
        return subprocess.run(['bash', str(ROOT / 'host' / script), *args], env=env, capture_output=True, text=True)
    result = run('ax-env.sh', '--profile', str(path))
    assert result.returncode == 0, result.stderr
    env['FAKE_FAIL'] = '1'
    failed = run('ax-deploy.sh')
    assert failed.returncode == 1, failed.stderr
    pending = tmp_path / 'state/deploy/pending.json'
    assert json.loads(pending.read_text())['status'] == 'pending'
    env['FAKE_FAIL'] = '0'
    succeeded = run('ax-deploy.sh')
    assert succeeded.returncode == 0, succeeded.stderr
    assert json.loads(pending.read_text())['status'] == 'completed'
    calls = [json.loads(line) for line in (tmp_path / 'calls').read_text().splitlines()]
    builds = [c for c in calls if '--build-arg' in c]
    assert len(builds) == 2
    assert any(c[:3] == ['docker', 'image', 'tag'] and c[-1] == f'ax/core:{SHA[:12]}' for c in calls)
    assert not any('up' in c for c in calls)
    assert any(c[:3] == ['docker', 'network', 'ls'] for c in calls)
    assert any(any('/ax-ipam-' in arg and arg.endswith('.compose.yaml') for arg in c) for c in calls)
    assert not any(c[:2] == ['docker', 'network'] and c[2] != 'ls' for c in calls)
    again = run('ax-deploy.sh')
    assert again.returncode == 0, again.stderr
    after = [json.loads(line) for line in (tmp_path / 'calls').read_text().splitlines()]
    assert len([c for c in after if '--build-arg' in c]) == 2
