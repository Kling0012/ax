"""Opt-in real-Docker check for the non-root chat runner image."""
import os
import subprocess
import uuid

import pytest


@pytest.mark.skipif(not os.environ.get('AX_CHAT_TEST_IMAGE'), reason='real Docker image not requested')
def test_chat_image_imports_fixed_module_as_nonroot():
    image = os.environ['AX_CHAT_TEST_IMAGE']
    name = 'ax-chat-permission-' + uuid.uuid4().hex[:12]
    command = ['sudo', '-n', 'docker']
    created = subprocess.run(command + ['create', '--name', name, '--network', 'none',
        '--user', f'{os.getuid()}:{os.getgid()}', '--entrypoint', 'python', image,
        '-P', '-c', 'import axcommon.chat_json; print("chat-module-readable")'],
        capture_output=True, text=True, timeout=60)
    assert created.returncode == 0, created.stderr
    started = subprocess.run(command + ['start', '-a', name], capture_output=True, text=True, timeout=60)
    assert started.returncode == 0, started.stderr
    assert started.stdout.strip() == 'chat-module-readable'
    # Keep the stopped container for audit; no cleanup or prune.


@pytest.mark.skipif(not os.environ.get('AX_VERIFY_TEST_IMAGE'), reason='real Docker image not requested')
def test_verify_image_checks_synthetic_report_as_nonroot():
    import json
    from pathlib import Path
    import tempfile

    root = Path(tempfile.mkdtemp(prefix='ax-verify-e2e-', dir=os.environ['TMPDIR']))
    (root / 'sample.txt').write_text('synthetic fixture\n')
    (root / 'report.json').write_text(json.dumps({
        'type': 'ax.research-report.v1', 'revision': 'a' * 40,
        'findings': [{'statement': 'fixture', 'citations': [
            {'path': 'sample.txt', 'line_start': 1, 'line_end': 1}]}],
    }))
    name = 'ax-verify-permission-' + uuid.uuid4().hex[:12]
    command = ['sudo', '-n', 'docker']
    created = subprocess.run(command + ['create', '--name', name, '--network', 'none', '--read-only',
        '--user', f'{os.getuid()}:{os.getgid()}', '--mount', f'type=bind,src={root},dst=/fixture,readonly',
        '--entrypoint', 'python', os.environ['AX_VERIFY_TEST_IMAGE'], '-P', '/opt/ax/read_verify.py',
        '--revision', 'a' * 40, '--report', '/fixture/report.json', '--snapshot', '/fixture'],
        capture_output=True, text=True, timeout=60)
    assert created.returncode == 0, created.stderr
    started = subprocess.run(command + ['start', '-a', name], capture_output=True, text=True, timeout=60)
    assert started.returncode == 0, started.stderr
    # Keep both the stopped container and the fixture; no cleanup or prune.
