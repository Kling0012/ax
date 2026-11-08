"""Fail-closed publication scanners and the S-8 review injection point."""
from __future__ import annotations

import bisect
import ipaddress
import hashlib
import os
import json
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from axcommon.redact import Redactor
from .publication import SelectedReviewer


def _apply_allowlist(target, findings):
    """Allow only exact rule, repository path and complete line combinations."""
    try:
        path = Path(os.environ.get('AX_PUBLICATION_ALLOWLIST', '/ax/config/publish-allowlist.json'))
        entries = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(entries, list):
            return findings
        allowed = {(e['rule'], e['path'], e['sha256']) for e in entries
                   if isinstance(e, dict) and all(isinstance(e.get(k), str) for k in ('rule', 'path', 'sha256'))}
    except (OSError, ValueError, TypeError, KeyError):
        return findings
    history = {}
    current = None
    in_hunk = False
    for number, line in enumerate(target.history.splitlines(), 1):
        if line.startswith('diff --git '):
            current = None
            in_hunk = False
        elif not in_hunk and line.startswith('+++ b/'):
            current = line[6:]
        elif line.startswith('@@ '):
            in_hunk = True
        elif in_hunk and current is not None and line.startswith(('+', '-')):
            history[number] = (current, line[1:])
    for finding in findings:
        match = re.fullmatch(r'(file:(\d+)|history):line:(\d+)', finding['location'])
        if not match:
            continue
        number = int(match.group(3))
        if match.group(2):
            index = int(match.group(2)) - 1
            if index >= len(target.files):
                continue
            item = target.files[index]
            lines = item.text.splitlines()
            if number > len(lines):
                continue
            candidate = (item.path, lines[number - 1])
        else:
            candidate = history.get(number)
            if candidate is None:
                continue
        name, line = candidate
        digest = hashlib.sha256(line.encode('utf-8')).hexdigest()
        key = (finding['type'], name, digest)
        if key in allowed:
            finding['status'] = 'allowed'
            finding['allow_key'] = {'rule': key[0], 'path': key[1], 'sha256': key[2]}
    return findings


class SecretScanner:
    def __init__(self, pii_path, *, index_path=None, config_path=None):
        self.redactor = Redactor(pii_list_path=pii_path, index_path=index_path, config_path=config_path)
        self.binary = shutil.which('gitleaks')

    def _gitleaks(self, text):
        if not self.binary:
            return []
        result = subprocess.run([self.binary, 'stdin', '--no-banner', '--no-color',
                                 '--redact=100', '--ignore-gitleaks-allow', '--log-level=error',
                                 '--report-format=json', '--report-path=-'],
                                input=text.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd='/', env={'PATH': '/usr/bin:/bin'}, timeout=60)
        if result.returncode not in (0, 1):
            raise ValueError('gitleaks_unavailable')
        report = json.loads(result.stdout)
        if not isinstance(report, list) or (result.returncode == 1 and not report):
            raise ValueError('gitleaks_invalid_response')
        return report

    def _gitleaks_files(self, sources):
        if not self.binary:
            return [[] for _ in sources]
        directory = Path(tempfile.mkdtemp(prefix='gate-scan-'))
        for index, (_, text) in enumerate(sources):
            (directory / str(index)).write_bytes(text.encode())
        result = subprocess.run([self.binary, 'dir', str(directory), '--no-banner', '--no-color',
                                 '--redact=100', '--ignore-gitleaks-allow', '--log-level=error',
                                 '--report-format=json', '--report-path=-'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd='/', env={'PATH': '/usr/bin:/bin'}, timeout=600)
        if result.returncode not in (0, 1):
            raise ValueError('gitleaks_unavailable')
        report = json.loads(result.stdout)
        if not isinstance(report, list) or (result.returncode == 1 and not report):
            raise ValueError('gitleaks_invalid_response')
        grouped = [[] for _ in sources]
        for hit in report:
            path = hit.get('File')
            if not isinstance(path, str):
                raise ValueError('gitleaks_invalid_response')
            file_path = Path(path)
            if file_path.parent != directory or not file_path.name.isdecimal():
                raise ValueError('gitleaks_invalid_response')
            index = int(file_path.name)
            if index >= len(grouped) or str(index) != file_path.name:
                raise ValueError('gitleaks_invalid_response')
            grouped[index].append(hit)
        return grouped

    def scan(self, text):
        if not self.redactor.reload_pii_list():
            raise ValueError('pii_list_unavailable')
        hits = self.redactor.scan(text)
        if self._gitleaks(text):
            hits.append('gitleaks')
        return hits

    def scan_findings(self, target):
        if not self.redactor.reload_pii_list():
            raise ValueError('pii_list_unavailable')
        return self.findings(target)

    def findings(self, target):
        # Ordinals avoid recording a path that itself contains sensitive data.
        findings = []
        eligible_findings = []
        sources = []
        for i, item in enumerate(target.files):
            sources.extend([(f'file:{i+1}', item.path, False), (f'file:{i+1}', item.text, True)])
            if item.binary is not None:
                sources.append((f'file:{i+1}', item.binary.decode('latin-1'), False))
        file_hits = self._gitleaks_files([(loc, text) for loc, text, _ in sources])
        # A diff marker is not part of the source line. Keep its width so
        # offsets and line numbers still refer to the original history.
        in_hunk = False
        history_lines = []
        for line in target.history.splitlines(keepends=True):
            if line.startswith('diff --git '):
                in_hunk = False
            elif line.startswith('@@ '):
                in_hunk = True
            elif in_hunk and line.startswith(('+', '-', ' ')):
                line = ' ' + line[1:]
            history_lines.append(line)
        sources.append(('history', ''.join(history_lines), True))
        file_hits.append(self._gitleaks(target.history))
        for (location, text, eligible), gitleaks_hits in zip(sources, file_hits):
            start = len(findings)
            newlines = [m.start() for m in re.finditer("\n", text)]
            for hit in self.redactor.scan(text):
                findings.append({'location': f'{location}:line:{bisect.bisect_left(newlines, hit.start) + 1}',
                                 'type': hit.category + ':' + hit.rule})
            for hit in gitleaks_hits:
                line = hit.get('StartLine')
                if type(line) is not int or line < 1:
                    raise ValueError('gitleaks_invalid_response')
                findings.append({'location': f'{location}:line:{line}', 'type': 'token:gitleaks'})
            if eligible:
                eligible_findings.extend(findings[start:])
        _apply_allowlist(target, eligible_findings)
        return findings


class InternalScanner:
    def __init__(self, hosts_path, terms_path):
        self.hosts_path, self.terms_path = Path(hosts_path), Path(terms_path)

    def __call__(self, target):
        return not any(f.get('status') != 'allowed' for f in self.findings(target))

    def findings(self, target):
        words = []
        for path in (self.hosts_path, self.terms_path):
            words += [line.strip().casefold() for line in path.read_text(encoding='utf-8').splitlines()
                      if line.strip() and not line.lstrip().startswith('#')]
        sources = [(f'file:{i+1}', text, eligible) for i, f in enumerate(target.files)
                   for text, eligible in ((f.path, False), (f.text, True), (f.binary.decode('latin-1') if f.binary is not None else '', False))]
        sources.append(('history', target.history, True))
        findings = []
        eligible_findings = []
        for location, text, eligible in sources:
            start = len(findings)
            newlines = [m.start() for m in re.finditer("\n", text)]
            matches = [(m, 'tailnet') for m in re.finditer(r'(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net\b', text)]
            for m in re.finditer(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', text):
                try:
                    if ipaddress.ip_address(m.group()) in ipaddress.ip_network('100.64.0.0/10'):
                        matches.append ( (m, 'internal_ip') )
                except ValueError:
                    pass
            for word in words:
                for m in re.finditer(re.escape(word), text, re.IGNORECASE):
                    matches.append((m, 'internal_term'))
            findings.extend({'location': f'{location}:line:{bisect.bisect_left(newlines, m.start()) + 1}',
                             'type': 'internal:' + kind} for m, kind in matches)
            if eligible:
                eligible_findings.extend(findings[start:])
        _apply_allowlist(target, eligible_findings)
        return findings


def _tiff_has_gps(data):
    if len(data) < 8 or data[:2] not in (b'II', b'MM'):
        raise ValueError('invalid_exif')
    order = '<' if data[:2] == b'II' else '>'
    def u16(offset):
        return struct.unpack_from(order + 'H', data, offset)[0]
    def u32(offset):
        return struct.unpack_from(order + 'I', data, offset)[0]
    if u16(2) != 42:
        raise ValueError('invalid_exif')
    offset = u32(4)
    if offset < 8 or offset + 2 > len(data):
        raise ValueError('invalid_exif')
    count = u16(offset)
    if offset + 2 + count * 12 + 4 > len(data):
        raise ValueError('invalid_exif')
    return any(u16(offset + 2 + n * 12) == 0x8825 for n in range(count))


def _image_has_gps(data):
    if data.startswith(b'\xff\xd8'):
        pos = 2
        while pos < len(data):
            if data[pos] != 255:
                raise ValueError('invalid_jpeg')
            while pos < len(data) and data[pos] == 255:
                pos += 1
            if pos >= len(data):
                raise ValueError('invalid_jpeg')
            marker = data[pos]; pos += 1
            if marker in (0xd9, 0xda):
                return False
            if marker == 1 or 0xd0 <= marker <= 0xd7:
                continue
            if pos + 2 > len(data):
                raise ValueError('invalid_jpeg')
            size = int.from_bytes(data[pos:pos+2], 'big')
            if size < 2 or pos + size > len(data):
                raise ValueError('invalid_jpeg')
            segment = data[pos+2:pos+size]
            if marker == 0xe1 and segment.startswith(b'Exif\0\0') and _tiff_has_gps(segment[6:]):
                return True
            pos += size
        raise ValueError('invalid_jpeg')
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        pos = 8
        while pos + 12 <= len(data):
            size = int.from_bytes(data[pos:pos+4], 'big')
            end = pos + 12 + size
            if end > len(data):
                raise ValueError('invalid_png')
            kind = data[pos+4:pos+8]
            if kind == b'eXIf' and _tiff_has_gps(data[pos+8:pos+8+size]):
                return True
            if kind == b'IEND':
                return False
            pos = end
        raise ValueError('invalid_png')
    if data[:2] in (b'II', b'MM'):
        return _tiff_has_gps(data)
    raise ValueError('unsupported_binary')


class ImageGPSScanner:
    def findings(self, target):
        return [{'location': f'file:{i+1}:line:1', 'type': 'image:gps'}
                for i, item in enumerate(target.files)
                if item.binary is not None and _image_has_gps(item.binary)]

    def __call__(self, target):
        return not self.findings(target)


image_gps_clear = ImageGPSScanner()


class S8ReviewerSelector:
    """Inject a ledger selector and executor; never choose a model locally."""
    def __init__(self, select_model, execute_model):
        self.select_model, self.execute_model = select_model, execute_model

    def select(self, request):
        model = self.select_model(request.payload())
        if not isinstance(model, dict):
            return None
        ident, family = model.get('id'), model.get('family')
        if not isinstance(ident, str) or not ident or not isinstance(family, str) or not family:
            return None
        if any(x in (ident + ' ' + family).casefold() for x in ('claude', 'anthropic', 'sonnet', 'opus', 'haiku')):
            return None
        return SelectedReviewer(ident, lambda payload: self.execute_model(model, payload))
