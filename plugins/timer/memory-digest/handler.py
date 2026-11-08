"""Read-only memory digest with immutable, versioned Markdown outputs."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3

from axcommon.redact import from_environment

JST = timezone(timedelta(hours=9))


def schedule(knobs):
    value = knobs.get('u_digest_time', '23:50')
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('invalid_digest_time')
    enabled = knobs.get('enabled', True)
    if type(enabled) is not bool:
        raise ValueError('invalid_digest_enabled')
    hour, minute = map(int, value.split(':'))
    return {'enabled': enabled, 'kind': 'cron', 'cron': f'{minute} {(hour - 9) % 24} * * *'}


class MemoryDigest:
    def __init__(self, directory, memory, aliases, redactor=None):
        if not aliases:
            raise ValueError('digest_aliases_unavailable')
        self.directory, self.memory, self.aliases = Path(directory), memory, aliases
        self.redactor = redactor or from_environment()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.directory / 'digest.sqlite'))
        self.db.execute('CREATE TABLE IF NOT EXISTS artifacts (day TEXT, version TEXT, project TEXT, path TEXT, PRIMARY KEY(day,version,project))')
        self.db.commit()

    def _records(self, day):
        start = datetime.combine(day, time.min, JST)
        end = start + timedelta(days=1)
        params = {'field': 'created_at', 'limit': 1000, 'since': start.isoformat(), 'until': end.isoformat()}
        records, seen = {}, set()
        while True:
            response = self.memory.get('/v1/memories', params=params)['memories']
            if not isinstance(response, dict) or response.get('isError'):
                raise ValueError('memory_list_failed')
            data = response.get('structuredContent')
            if not isinstance(data, dict):
                data = json.loads(''.join(x.get('text', '') for x in response.get('content', []) if x.get('type') == 'text'))
            if isinstance(data.get('result'), dict):
                data = data['result']
            if not isinstance(data.get('items'), list):
                raise ValueError('invalid_memory_list')
            for record in data['items']:
                ident = record['memory_id']
                stamp = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError('invalid_memory_timestamp')
                if start <= stamp < end:
                    records[ident] = record
            cursor = data.get('next_cursor')
            if not cursor:
                return [records[k] for k in sorted(records)]
            if cursor in seen or not data['items']:
                raise ValueError('memory_pagination_stalled')
            seen.add(cursor)
            params['cursor'] = cursor

    def run(self, day, knobs=None):
        if not schedule(knobs or {})['enabled']:
            return {'result': 'disabled', 'artifacts': {}}
        day = date.fromisoformat(day)
        records = self._records(day)
        groups = {}
        for record in records:
            text = self.redactor.redact_text(str(record.get('content') or '')).text
            tags = ' '.join(str(t) for t in record.get('tags', []))
            haystack = (text + ' ' + tags).casefold()
            project = next((name for name, terms in self.aliases.items()
                            if any(re.search(r'(?<![a-z0-9])' + re.escape(term.casefold()) + r'(?![a-z0-9])', haystack) for term in terms)), 'その他')
            project = self.redactor.redact_text(project).text
            line = ' '.join(text.split()).replace('<', '&lt;').replace('>', '&gt;')
            groups.setdefault(project, []).append(line)
        version = sha256(json.dumps(records, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        outputs = {'daily': '# 毎日のまとめ ' + day.isoformat() + '\n\n' + ''.join('## ' + p + '\n\n' + ''.join('- ' + x + '\n' for x in entries) + '\n' for p, entries in sorted(groups.items()))}
        for project, entries in groups.items():
            outputs['project:' + project] = '# ' + project + '\n\n' + ''.join('- ' + x + '\n' for x in entries)
        paths = {}
        for key, text in outputs.items():
            clean = self.redactor.redact_text(text).text
            if self.redactor.scan(clean):
                raise ValueError('digest_redaction_failed')
            name = day.isoformat() + '-' + version + '-' + sha256(key.encode()).hexdigest()[:16] + '.md'
            path = self.directory / name
            try:
                with path.open('x', encoding='utf-8') as f:
                    f.write(clean)
            except FileExistsError:
                if path.read_text(encoding='utf-8') != clean:
                    raise ValueError('digest_artifact_conflict')
            paths[key] = str(path)
        with self.db:
            for key, path in paths.items():
                self.db.execute('INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?, ?)', (day.isoformat(), version, key, path))
        return {'result': 'generated', 'artifacts': paths, 'version': version}


async def run(ctx):
    factory = ctx.clients['memory_digest']
    instance, day, knobs = factory.open(ctx)
    try:
        return instance.run(day, knobs)
    finally:
        if callable(getattr(factory, "close", None)):
            factory.close(instance)
