"""Network and proxy inspect behaviour of the fake Docker CLI."""
import json
import os
import subprocess
import sys

from tests.b6b.helpers import FAKE_DOCKER


def test_network_proxy_inspection_and_dns(tmp_path):
    env = {**os.environ, 'FAKE_DOCKER_DIR': str(tmp_path)}
    def run(*args, ok=True):
        result = subprocess.run([sys.executable, str(FAKE_DOCKER), *args], env=env, capture_output=True, text=True)
        assert (result.returncode == 0) == ok, result.stderr
        return result.stdout.strip()

    run('network', 'create', '--internal', 'inside')
    run('network', 'create', 'outside')
    run('create', '--name', 'proxy', '--network', 'inside', '--network-alias', 'verify-proxy',
        '--user', '1000:1000', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--sysctl', 'net.ipv4.ip_forward=0',
        '--sysctl', 'net.ipv6.conf.all.forwarding=0', '-v', '/policy:/policy/allow:ro',
        '-e', 'EGRESS_FIXED_ALLOW_FILE=/policy/allow', '--entrypoint', 'python', 'image', '/proxy.py')
    run('start', 'proxy')
    run('network', 'connect', 'outside', 'proxy')
    inside = json.loads(run('network', 'inspect', 'inside'))[0]
    outside = json.loads(run('network', 'inspect', 'outside'))[0]
    assert inside['Internal'] is True and outside['Internal'] is False
    assert [v['Name'] for v in inside['Containers'].values()] == ['proxy']
    assert [v['Name'] for v in outside['Containers'].values()] == ['proxy']
    proxy = json.loads(run('inspect', '--type', 'container', 'proxy'))[0]
    assert set(proxy['NetworkSettings']['Networks']) == {'inside', 'outside'}
    assert proxy['NetworkSettings']['Networks']['inside']['Aliases'] == ['verify-proxy']
    assert proxy['Config']['Entrypoint'] == ['python'] and proxy['Config']['Cmd'] == ['/proxy.py']
    assert proxy['Config']['User'] == '1000:1000'
    assert proxy['HostConfig']['Sysctls']['net.ipv4.ip_forward'] == '0'
    assert proxy['Mounts'][0]['RW'] is False
    assert run('exec', 'proxy', 'cat', '/proc/sys/net/ipv4/ip_forward') == '0'
    assert run('exec', 'proxy', 'cat', '/proc/sys/net/ipv6/conf/all/forwarding') == '0'
    run('create', '--name', 'job', '--network', 'inside', '--dns', '127.0.0.1', 'image')
    job = json.loads(run('inspect', '--type', 'container', 'job'))[0]
    assert job['HostConfig']['Dns'] == ['127.0.0.1']
    run('network', 'connect', 'missing', 'proxy', ok=False)
    run('exec', 'proxy', 'unexpected', 'command', ok=False)
