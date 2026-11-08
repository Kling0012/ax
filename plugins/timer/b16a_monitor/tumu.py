"""Read a bounded tail of the latest Android Tumu trace over SSH and ADB."""
from datetime import datetime
import re
import shlex
import subprocess


class TumuSSHReader:
    def __init__(self, targets, *, runner=subprocess.run):
        self.targets = targets
        self.runner = runner

    def read(self, target):
        config = self.targets.get(target)
        if not isinstance(config, dict):
            return {'status': 'error'}
        host, user = (config.get(k) for k in ('host', 'user'))
        port, key = config.get('port', 22), config.get('identity_file')
        serial = config.get('adb_serial')
        threshold = config.get('stall_threshold_s', 300)
        if (not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*', host)
                or not isinstance(user, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', user)
                or type(port) is not int or not 1 <= port <= 65535
                or not isinstance(key, str) or not key.startswith('/')
                or (serial is not None and (not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9._:-]+', serial)))
                or type(threshold) is not int or threshold <= 0):
            return {'status': 'error'}
        script = ('cd /sdcard/Android/data/com.tumu.autoplay/files/devtrace || exit 1; '
                  'f=$(ls -t trace-*.log 2>/dev/null | head -1); [ -n "$f" ] || exit 1; '
                  'date +%s || exit 1; tail -c 3000000 "$f"')
        adb = ['adb'] + (['-s', serial] if serial else []) + ['shell', script]
        command = ' '.join(shlex.quote(part) for part in adb)
        argv = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ClearAllForwardings=yes', '-o', 'IdentitiesOnly=yes',
                '-o', 'ConnectTimeout=10', '-p', str(port), '-i', key,
                user + '@' + host, command]
        try:
            result = self.runner(argv, input=b'', stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=30, check=False)
            if result.returncode == 255:
                return {'status': 'unreachable'}
            if result.returncode or len(result.stdout) > 3000100:
                return {'status': 'error'}
            lines = result.stdout.decode('utf-8', errors='replace').splitlines()
            now = int(lines[0].strip())
            tick = None
            for line in lines[1:]:
                if 'autoplay tick' in line and 'adbd' not in line:
                    tick = line
            if tick is None:
                return {'status': 'error'}
            columns = tick.split('\t')
            if len(columns) != 7 or columns[4] != 'tick' or columns[5] != 'autoplay tick':
                return {'status': 'error'}
            at = datetime.fromisoformat(columns[0])
            if at.tzinfo is None:
                return {'status': 'error'}
            age = now - int(at.timestamp())
            if age < 0:
                return {'status': 'error'}
            return {'status': 'ok', 'stalled': age > threshold or 'loop stopped' in columns[6]}
        except (OSError, subprocess.TimeoutExpired):
            return {'status': 'unreachable'}
        except (ValueError, TypeError, IndexError, UnicodeError, OverflowError):
            return {'status': 'error'}
