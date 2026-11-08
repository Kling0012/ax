"""Read-only checks exposed through the restricted environment entrypoint."""
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import shutil
import subprocess


def _transfer(environ, request):
    try:
        data = json.loads(request)
        comment = data['key_comment']
        if not isinstance(comment, str) or not comment or len(comment) > 256 or any(c in comment for c in '\r\n'):
            return 'unknown'
        connection = environ.get('SSH_CONNECTION', '').split()
        if len(connection) != 4:
            return 'unknown'
        address = str(ipaddress.ip_address(connection[0]))
        ipaddress.ip_address(connection[2])
        if not all(x.isdigit() and 0 < int(x) < 65536 for x in (connection[1], connection[3])):
            return 'unknown'
        host = socket.gethostbyaddr(address)[0]
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
            return 'unknown'
        user_result = subprocess.run(['id', '-un'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, timeout=15, check=False)
        user = user_result.stdout.decode('utf-8', 'strict').strip()
        if user_result.returncode or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', user):
            return 'unknown'
        result = subprocess.run(['sudo', '-n', 'sshd', '-T', '-C', f'user={user},host={host},addr={address}'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=15, check=False)
        if result.returncode:
            return 'unknown'
        settings = {}
        for line in result.stdout.decode('utf-8', 'strict').splitlines():
            key, sep, value = line.partition(' ')
            if not sep or not re.fullmatch(r'[a-z][a-z0-9]*', key) or not value.strip():
                return 'unknown'
            if key in ('disableforwarding', 'allowtcpforwarding', 'authorizedkeysfile'):
                if key in settings:
                    return 'unknown'
                settings[key] = value.strip()
        if settings.get('disableforwarding') not in ('yes', 'no') or settings.get('allowtcpforwarding') not in ('yes', 'no', 'local', 'remote'):
            return 'unknown'
        paths = settings.get('authorizedkeysfile', '').split()
        if not paths or 'none' in [path.lower() for path in paths]:
            return 'unknown'
        home = environ.get('HOME', '')
        if not home or not os.path.isabs(home):
            return 'unknown'
        matches = []
        for configured_path in paths:
            path = configured_path.replace('%h', home).replace('%u', user)
            if '%' in path:
                return 'unknown'
            path = Path(path) if os.path.isabs(path) else Path(home) / path
            try:
                file = path.open(encoding='utf-8')
            except FileNotFoundError:
                continue
            with file:
                for line in file:
                    if not line.strip() or line.lstrip().startswith('#'):
                        continue
                    # Capture only options and comment; never retain or emit the key body.
                    match = re.fullmatch(r'\s*(?:(?P<options>(?:[^\s"\\]|\\.|"(?:[^"\\]|\\.)*")+)\s+)?(?:ssh-[\w@.-]+|ecdsa-[\w@.-]+|sk-[\w@.-]+)\s+\S+\s+(?P<comment>[^\r\n]+?)\s*', line)
                    if match is None:
                        return 'unknown'
                    if match.group('comment') == comment:
                        options = match.group('options') or ''
                        lexer = shlex.shlex(options, posix=True)
                        lexer.whitespace = ','
                        lexer.whitespace_split = True
                        lexer.commenters = ''
                        matches.append(list(lexer))
        if len(matches) != 1:
            return 'unknown'
        options = matches[0]
        if any(option == 'restrict' or option.startswith('command=') and len(option) > len('command=') for option in options):
            return 'ok'
        if not options and settings['disableforwarding'] == 'no' and settings['allowtcpforwarding'] == 'yes':
            return 'ng'
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, subprocess.SubprocessError):
        pass
    return 'unknown'


def inspect(home, environ, request):
    checks = {'transfer': 'unknown', 'commands': 'unknown', 'disk': 'unknown', 'sudo': 'unknown'}
    required = ('python3', 'tar', 'df', 'sudo', 'sshd')
    checks['commands'] = 'ok' if all(shutil.which(name) for name in required) else 'ng'
    try:
        result = subprocess.run(['df', '-Pk', '--', str(home)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
        lines = result.stdout.decode('utf-8', 'strict').splitlines()
        fields = lines[-1].split() if len(lines) >= 2 else []
        if result.returncode == 0 and len(fields) >= 6 and fields[-3].isdigit():
            checks['disk'] = 'ok' if int(fields[-3]) > 0 else 'ng'
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    try:
        result = subprocess.run(['sudo', '-n', 'sshd', '-T'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
        text = result.stdout.decode('utf-8', 'strict')
        settings = {}
        for line in text.splitlines():
            key, sep, value = line.partition(' ')
            if sep and re.fullmatch(r'[a-z][a-z0-9]*', key):
                settings.setdefault(key, value.strip())
        if result.returncode == 0 and 'passwordauthentication' in settings:
            checks['sudo'] = 'ok'
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    checks['transfer'] = _transfer(environ, request)
    return {'ok': True, 'checks': checks}
