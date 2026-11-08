#!/usr/bin/env python3
"""Run one JSON-described command inside a sandbox."""
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

LIMIT = 65536
GRACE = 1.0


def artifact_path(root, name):
    if not isinstance(name, str) or not name or '\x00' in name:
        raise ValueError('invalid artifact path')
    path = Path(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('artifact path outside work directory')
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError('artifact path outside work directory')
    return resolved


def run(spec):
    if not isinstance(spec, dict) or not isinstance(spec.get('job_id'), str) or not spec['job_id']:
        raise ValueError('invalid job id')
    directory = spec.get('workdir')
    if not isinstance(directory, str) or not os.path.isabs(directory) or '..' in Path(directory).parts:
        raise ValueError('invalid work directory')
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('invalid work directory')
    command = spec.get('command')
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and '\x00' not in x for x in command):
        raise ValueError('invalid command')
    timeout = spec.get('timeout_seconds')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('invalid timeout')
    names = spec.get('artifacts')
    if not isinstance(names, list):
        raise ValueError('invalid artifacts')
    for name in names:
        artifact_path(root, name)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(command, cwd=root, stdout=out, stderr=err, start_new_session=True)
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=GRACE)
            except subprocess.TimeoutExpired:
                pass
            # Descendants may survive their leader, so always signal the process group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        def tail(stream):
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - LIMIT))
            return stream.read().decode('utf-8', 'replace')
        stdout, stderr = tail(out), tail(err)
    artifacts = []
    for name in names:
        path = artifact_path(root, name)
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as f:
                for chunk in iter(lambda: f.read(131072), b''):
                    digest.update(chunk)
            artifacts.append({'path': name, 'sha256': digest.hexdigest()})
    return {'exit_code': process.returncode, 'timed_out': timed_out, 'stdout': stdout,
            'stderr': stderr, 'artifacts': artifacts}


def main():
    try:
        result = run(json.loads(sys.argv[1]))
    except (ValueError, TypeError, OSError, IndexError) as exc:
        result = {'exit_code': 1, 'timed_out': False, 'stdout': '', 'stderr': str(exc), 'artifacts': []}
    print(json.dumps(result, ensure_ascii=True, separators=(',', ':')))


if __name__ == '__main__':
    main()
