"""Conservative interpretation of a read-only SSH environment probe."""
from collections.abc import Mapping

NAMES = ('ssh', 'transfer', 'commands', 'disk', 'sudo')


def probe(execute):
    checks = {name: 'unknown' for name in NAMES}
    try:
        status, response = execute()
    except Exception:
        status, response = 'unknown', None
    if status == 'unreachable':
        checks['ssh'] = 'ng'
    elif status == 'connected':
        checks['ssh'] = 'ok'
        remote = response.get('checks') if isinstance(response, Mapping) and response.get('ok') is True else None
        if isinstance(remote, Mapping):
            for name in NAMES[1:]:
                if remote.get(name) in ('ok', 'ng', 'unknown'):
                    checks[name] = remote[name]
    return {'ok': all(value == 'ok' for value in checks.values()), 'checks': checks}
