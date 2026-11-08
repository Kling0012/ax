"""Restricted environment jobs served by the B6 SSH entry point."""
import io
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

JOB_ID = re.compile(r"[A-Za-z0-9-]{1,128}\Z")


class JobError(Exception):
    def __init__(self, reason, missing=False):
        self.reason = reason
        self.missing = missing


def job_path(home, job_id):
    if not JOB_ID.fullmatch(job_id):
        raise JobError('invalid_job_id')
    return Path(home) / 'jobs' / job_id


def submit(home, job_id, raw):
    path = job_path(home, job_id)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
            members = archive.getmembers()
            if len(members) != 1 or members[0].name != 'spec.json' or not members[0].isfile() or members[0].size > 1024 * 1024:
                raise JobError('invalid_archive')
            spec = json.load(archive.extractfile(members[0]))
    except (tarfile.TarError, ValueError, OSError, UnicodeError):
        raise JobError('invalid_archive') from None
    if not isinstance(spec, dict) or spec.get('job_id') != job_id or not isinstance(spec.get('idempotency_key'), str) or not spec['idempotency_key']:
        raise JobError('invalid_spec')
    entry = spec.get('entry')
    if not isinstance(entry, dict) or entry.get('kind') != 'entrypoint' or entry.get('name') not in ('audit-readonly', 'hibi-state') or entry.get('args', {}) != {} or set(entry) - {'kind', 'name', 'args'}:
        raise JobError('invalid_entry')
    if spec.get('secrets', []) or spec.get('inputs', []):
        raise JobError('unsupported_input')
    if entry['name'] == 'hibi-state' and (spec.get('outputs') != ['state.json'] or spec.get('requires') != ['hibi-state-read'] or spec.get('budget_min') != 2 or spec.get('irreversible') is not False):
        raise JobError('invalid_spec')
    path.parent.mkdir(parents=True, exist_ok=True)
    index = Path(home) / 'index' / 'idem'
    index.mkdir(parents=True, exist_ok=True)
    key_hash = hashlib.sha256(spec['idempotency_key'].encode('utf-8')).hexdigest()
    # The key lock serializes reservation only; a retry can observe running.
    with (index / (key_hash + '.lock')).open('a+', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record_path = index / key_hash
        if record_path.exists():
            saved = record_path.read_text(encoding='utf-8')
            try:
                previous = json.loads(saved)
            except ValueError:
                return {'ok': False, 'job_id': job_id, 'state': 'unknown', 'exit_code': None}
            if previous != spec:
                raise JobError('idempotency_conflict')
            try:
                prior_spec = json.loads((path / 'spec.json').read_text(encoding='utf-8'))
            except (OSError, ValueError, UnicodeError):
                return {'ok': False, 'job_id': job_id, 'state': 'unknown', 'exit_code': None}
            if prior_spec != spec:
                raise JobError('job_exists')
            return status(home, job_id, missing_unknown=True)
        # Persist the reservation before any execution, including a crash in setup.
        with record_path.open('x', encoding='utf-8') as record:
            record.write(json.dumps(spec, sort_keys=True, separators=(',', ':')))
            record.flush()
            os.fsync(record.fileno())
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            raise JobError('job_exists') from None
        (path / 'out').mkdir(mode=0o700)
        (path / 'work').mkdir(mode=0o700)
        (path / 'spec.json').write_text(json.dumps(spec), encoding='utf-8')
        state = {'ok': True, 'job_id': job_id, 'state': 'running', 'exit_code': None}
        (path / 'state.json').write_text(json.dumps(state), encoding='utf-8')
    if entry['name'] == 'hibi-state':
        return run_hibi(path, state)
    try:
        result = subprocess.run(['sudo', '-n', 'sshd', '-T'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False, cwd=path / 'work')
        if result.returncode:
            raise OSError('audit unavailable')
        lines = result.stdout.decode('utf-8', 'strict').splitlines()
        if not lines or any(not re.fullmatch(r'[A-Za-z0-9_-]+ .+', line) for line in lines):
            raise OSError('invalid audit response')
        output = '# audit\n' + ''.join('sshd_effective\t' + line.replace(' ', '\t', 1) + '\n' for line in lines)
        (path / 'out' / 'audit.tsv').write_text(output, encoding='utf-8')
        state.update(state='succeeded', exit_code=0)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        (path / 'out' / 'audit.tsv').write_text('# audit\nCOVERAGE\tunavailable\n', encoding='utf-8')
        state.update(state='failed', exit_code=1, error={'class': 'unavailable', 'message': 'sshd effective settings unavailable'})
    tmp = path / 'state.new'
    tmp.write_text(json.dumps(state), encoding='utf-8')
    os.replace(tmp, path / 'state.json')
    return state



_HIBI_READ = r"""
import json, os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.expanduser('~/dev/hq-hibi-bridge'))
from bridge import Bridge, build_config, hq_v1_view
cfg = build_config({'HOME': os.path.expanduser('~')})
bridge = Bridge(cfg)
try:
    if bridge.client is not None:
        bridge.poll()
except Exception:
    pass
now = hq_v1_view(bridge.now_response())
day = hq_v1_view(bridge.day_response(None))
now = {k: now[k] for k in ('state', 'since', 'as_of') if k in now}
day = {'date': day.get('date'), 'segments': [{k: v for k, v in item.items() if k in ('start', 'end', 'state', 'category')} for item in day.get('segments', [])]}
print(json.dumps({'now': now, 'day': day, 'fetched_at': datetime.now(timezone.utc).isoformat()}, separators=(',', ':')))
"""


def run_hibi(path, state):
    import selectors
    import time
    limit = 64 * 1024
    process = None
    try:
        process = subprocess.Popen(['/usr/bin/python3', '-I', '-c', _HIBI_READ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=path / 'work', env={'HOME': str(Path.home()), 'PATH': '/usr/bin:/bin'})
        output = bytearray()
        deadline = time.monotonic() + 30
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise OSError('Hibi timed out')
                chunk = os.read(process.stdout.fileno(), min(8192, limit + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise OSError('Hibi output too large')
        if process.wait(timeout=max(.01, deadline - time.monotonic())) != 0:
            raise OSError('Hibi failed')
        json.loads(output)
        (path / 'out' / 'state.json').write_bytes(output)
        state.update(state='succeeded', exit_code=0)
    except (OSError, ValueError, subprocess.SubprocessError):
        state.update(state='failed', exit_code=1, error={'class': 'unavailable', 'message': 'Hibi state unavailable'})
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
    tmp = path / 'state.new'
    tmp.write_text(json.dumps(state), encoding='utf-8')
    os.replace(tmp, path / 'state.json')
    return state


def status(home, job_id, missing_unknown=False):
    path = job_path(home, job_id)
    try:
        data = json.loads((path / 'state.json').read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or data.get('job_id') != job_id
                or data.get('state') not in ('running', 'succeeded', 'failed', 'unknown')):
            raise ValueError('invalid state')
        return data
    except (FileNotFoundError, ValueError, UnicodeError, OSError):
        if not missing_unknown and not path.exists():
            raise JobError('job_not_found', True) from None
        return {'ok': False, 'job_id': job_id, 'state': 'unknown', 'exit_code': None}


def fetch(home, job_id, name=None):
    state = status(home, job_id)
    if state['state'] not in ('succeeded', 'failed'):
        raise JobError('job_not_finished')
    if name is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', name):
        raise JobError('invalid_artifact')
    out = job_path(home, job_id) / 'out'
    paths = [out / name] if name else sorted(out.iterdir())
    if any(not p.is_file() or p.is_symlink() for p in paths):
        raise JobError('artifact_not_found', True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as archive:
        for path in paths:
            if path.name == 'state.json':
                info = tarfile.TarInfo('state.json')
                info.size = path.stat().st_size
                with path.open('rb') as source:
                    archive.addfile(info, source)
            else:
                archive.add(path, arcname=path.name, recursive=False)
    return buf.getvalue()
