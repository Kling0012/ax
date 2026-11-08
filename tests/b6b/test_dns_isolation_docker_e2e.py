"""Opt-in Docker CLI test against a prepared, isolated synthetic DNS topology.

Set AX_DNS_PROOF_SUFFIX to the suffix of ax-dns-{job,control,canary,proxy}
containers. The control has the canary as its DNS; the job has 127.0.0.1.
The fake upstream is on 11.254.247.2 in an internal network only.
No container or network is created or removed by this test.
"""
import os
import subprocess

import pytest

SUFFIX = os.environ.get('AX_DNS_PROOF_SUFFIX')
pytestmark = [pytest.mark.realenv, pytest.mark.skipif(not SUFFIX, reason='requires prepared isolated Docker topology')]


def docker(*args, code=None):
    return subprocess.run(['sudo', '-n', 'docker', *args], input=code, text=True,
                          capture_output=True, check=True, timeout=20).stdout


def name(part):
    return f'ax-dns-{part}-{SUFFIX}'


def test_embedded_dns_forwards_only_to_job_loopback():
    assert '127.0.0.1' in docker('inspect', name('job'), '--format', '{{json .HostConfig.Dns}}')
    before = docker('logs', name('canary')).count('CANARY_QUERY')
    docker('exec', '-i', name('control'), 'python', '-', code='''
import socket
try: socket.getaddrinfo('control.proof.invalid', 443)
except OSError: pass
''')
    control = docker('logs', name('canary')).count('CANARY_QUERY')
    assert control > before
    result = docker('exec', '-i', name('job'), 'python', '-', code='''
import socket, threading
assert socket.getaddrinfo('verify-proxy', 3128)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
s.bind(('127.0.0.1', 53))
seen = []
def observe():
    try:
        data, addr = s.recvfrom(4096)
        seen.append(data)
        s.sendto(data[:2] + b'\\x81\\x83' + data[4:6] + b'\\x00' * 6 + data[12:], addr)
    except OSError: pass
t = threading.Thread(target=observe); t.start()
try: socket.getaddrinfo('observer.proof.invalid', 443)
except OSError: pass
t.join(); s.close()
assert seen, 'embedded DNS did not forward to the job loopback'
print('DNS_LOOPBACK_OK')
''')
    assert 'DNS_LOOPBACK_OK' in result
    assert docker('logs', name('canary')).count('CANARY_QUERY') == control


def test_isolated_job_proxy_and_direct_network_paths():
    remote_before = docker('logs', name('remote2'))
    result = docker('exec', '-i', name('job'), 'python', '-', code='''
import base64, socket

def proxy(target, body=b''):
    with socket.create_connection(('verify-proxy', 3128), timeout=3) as conn:
        auth = base64.b64encode(b'job-other:').decode()
        conn.sendall(f'CONNECT {target} HTTP/1.1\\r\\nHost: {target}\\r\\nProxy-Authorization: Basic {auth}\\r\\n\\r\\n'.encode() + body)
        data = b''
        while True:
            part = conn.recv(4096)
            if not part: return data
            data += part
r = proxy('allowed.example:443', b'GET / HTTP/1.0\\r\\n\\r\\n')
assert r.startswith(b'HTTP/1.1 200 ') and r.endswith(b'ok')
for target in ['other.example:443', 'allowed.example:3425', '127.0.0.1:443', '11.254.247.2:443']:
    assert proxy(target).startswith(b'HTTP/1.1 403 '), target
for host, port in [('verify-proxy', 3425), ('11.254.247.2', 443), ('11.254.247.4', 53)]:
    try: conn = socket.create_connection((host, port), timeout=1)
    except OSError: continue
    conn.close(); raise AssertionError((host, port))
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1)
try:
    s.sendto(b'dns-test', ('11.254.247.4', 53))
    try: s.recvfrom(1024); raise AssertionError('unexpected DNS response')
    except socket.timeout: pass
except OSError: pass
finally: s.close()
print('NETWORK_OK')
''')
    assert 'NETWORK_OK' in result
    assert docker('logs', name('remote2')) == remote_before
    forwarding = docker('exec', name('proxy'), 'python', '-c',
        "from pathlib import Path; print(Path('/proc/sys/net/ipv4/ip_forward').read_text().strip(), Path('/proc/sys/net/ipv6/conf/all/forwarding').read_text().strip())")
    assert forwarding.strip() == '0 0'
