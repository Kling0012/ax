"""Incremental conversation indexing; I/O is supplied by the runtime."""
from __future__ import annotations

import json
import re
import sqlite3

from axcommon.redact import from_environment
from axcommon.embedding import EmbeddingUnavailable


def schedule(knobs):
    value = knobs.get('u_index_time', '04:41')
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('invalid_index_time')
    enabled = knobs.get('enabled', True)
    if type(enabled) is not bool:
        raise ValueError('invalid_index_enabled')
    hour, minute = map(int, value.split(':'))
    return {'enabled': enabled, 'kind': 'cron', 'cron': f'{minute} {(hour - 9) % 24} * * *'}


class ConversationIndex:
    def __init__(self, path, source, resources, embedder, redactor=None):
        self.source, self.resources, self.embedder = source, resources, embedder
        if redactor is None:
            self.redactor = from_environment()
            if (self.redactor.index_path is None or self.redactor.pii_list_path is None
                    or not self.redactor.index_ok or not self.redactor.pii_list_ok):
                raise ValueError('redaction_unavailable')
        else:
            self.redactor = redactor
        self.db = sqlite3.connect(str(path))
        self.db.execute('CREATE TABLE IF NOT EXISTS docs (id TEXT PRIMARY KEY, version TEXT NOT NULL, text TEXT NOT NULL, embedding TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.commit()

    def run(self, provider, model):
        try:
            result = self._run(provider, model)
        except EmbeddingUnavailable as exc:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO state VALUES ('search_unavailable', ?)", (str(exc),))
            return {'result': 'unavailable', 'reason': str(exc)}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('embedding_model', ?)", (json.dumps([provider, model]),))
            self.db.execute("DELETE FROM state WHERE key IN ('search_unavailable', 'index_unavailable')")
        return result

    def _run(self, provider, model):
        # The resource registry must attest to the exact provider/model pair.
        if not self.resources.registered_model(provider, model):
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO state VALUES ('index_unavailable', ?)",
                                ('unregistered_embedding_model',))
            raise PermissionError('unregistered_embedding_model')
        previous = self.db.execute("SELECT value FROM state WHERE key='embedding_model'").fetchone()
        if previous and json.loads(previous[0]) != [provider, model]:
            raise EmbeddingUnavailable('embedding_model_changed')
        if not previous:
            if self.db.execute('SELECT 1 FROM docs LIMIT 1').fetchone():
                raise EmbeddingUnavailable('embedding_model_unknown')
            with self.db:
                self.db.execute("INSERT INTO state VALUES ('embedding_model', ?)", (json.dumps([provider, model]),))
        row = self.db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()
        cursor = json.loads(row[0]) if row else None
        for change in self.source.changes(cursor):
            ident, version, position, text = (change[k] for k in ('id', 'version', 'cursor', 'text'))
            if not all(isinstance(x, str) and x for x in (ident, version, text)):
                raise ValueError('invalid_history_record')
            current = self.db.execute('SELECT version FROM docs WHERE id=?', (ident,)).fetchone()
            if current is None or current[0] != version:
                if hasattr(self.embedder, "validate_text"):
                    self.embedder.validate_text(text)
                clean = self.redactor.redact_text(text).text
                # Never call an adapter with raw history or unregistered destinations.
                vector = self.embedder.embed(provider, model, clean)
                encoded = json.dumps(vector, allow_nan=False)
            with self.db:
                if current is None or current[0] != version:
                    self.db.execute('INSERT OR REPLACE INTO docs VALUES (?, ?, ?, ?)', (ident, version, clean, encoded))
                self.db.execute("INSERT OR REPLACE INTO state VALUES ('cursor', ?)", (json.dumps(position),))
        return {'result': 'indexed'}

    def search(self, query, limit=10):
        import math

        if not isinstance(query, str) or type(limit) is not int or limit < 1:
            raise ValueError('invalid_search')
        clean = self.redactor.redact_text(query)
        rows = list(self.db.execute('SELECT id, version, text, embedding FROM docs ORDER BY id'))

        def fallback(reason):
            items = [{'id': ident, 'version': version, 'text': text}
                     for ident, version, text, _ in rows if clean.text.casefold() in text.casefold()][:limit]
            return {'result': 'fallback', 'reason': reason, 'items': items}

        row = self.db.execute("SELECT value FROM state WHERE key='search_unavailable'").fetchone()
        if row:
            return fallback(row[0])
        if clean.changed:
            return fallback('sensitive_text')
        # Legacy injected adapters without an outbound inspection guard keep their
        # original local substring interface; they are never used for query embedding.
        if not callable(getattr(self.embedder, 'validate_text', None)):
            return [{'id': ident, 'version': version, 'text': text}
                    for ident, version, text, _ in rows if clean.text.casefold() in text.casefold()][:limit]
        model = self.db.execute("SELECT value FROM state WHERE key='embedding_model'").fetchone()
        if not model:
            return fallback('embedding_model_unavailable')
        try:
            provider, name = json.loads(model[0])
            self.embedder.validate_text(query)
            vector = self.embedder.embed(provider, name, query)
            if not vector or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
                raise ValueError('invalid vector')
            norm = math.sqrt(sum(x*x for x in vector))
            if not norm or not math.isfinite(norm):
                raise ValueError('invalid vector norm')
            result = []
            for ident, version, text, encoded in rows:
                other = json.loads(encoded)
                if (not isinstance(other, list) or len(other) != len(vector)
                        or any(type(x) not in (int, float) or not math.isfinite(x) for x in other)):
                    raise ValueError('invalid stored vector')
                other_norm = math.sqrt(sum(x*x for x in other))
                if not other_norm or not math.isfinite(other_norm):
                    raise ValueError('invalid stored vector norm')
                score = sum((a / norm) * (b / other_norm) for a, b in zip(vector, other))
                if not math.isfinite(score):
                    raise ValueError('invalid similarity')
                result.append({'id': ident, 'version': version, 'text': text, 'score': score})
            return sorted(result, key=lambda item: (-item['score'], item['id']))[:limit]
        except EmbeddingUnavailable as exc:
            return fallback(str(exc))
        except (ValueError, TypeError, OverflowError):
            return fallback('invalid_embedding')


async def run(ctx):
    # Runtime integration provides this factory with the owning plugin's credentials.
    factory = ctx.clients.get('conversation_index')
    if factory is None:
        return local_run()
    if hasattr(factory, 'run_async'):
        return await factory.run_async(ctx)
    instance, provider, model = factory.open(ctx)
    return instance.run(provider, model)


class LocalHistoryIndex:
    """Read mounted JSONL transcripts and keep only redacted text in persistent SQLite."""

    def __init__(self, history_dir, path, redactor):
        from pathlib import Path
        self.history_dir = Path(history_dir)
        self.redactor = redactor
        self.db = sqlite3.connect(str(path))
        self.db.execute('CREATE TABLE IF NOT EXISTS docs (id TEXT PRIMARY KEY, version TEXT NOT NULL, text TEXT NOT NULL)')
        self.db.commit()

    def run(self):
        import hashlib
        if not self.history_dir.is_dir():
            raise ValueError('history_source_unavailable')
        for path in sorted(self.history_dir.rglob('*.jsonl')):
            if path.is_symlink() or not path.is_file():
                continue
            ident = path.relative_to(self.history_dir).as_posix()
            data = path.read_bytes()
            version = hashlib.sha256(data).hexdigest()
            if self.db.execute('SELECT version FROM docs WHERE id=?', (ident,)).fetchone() == (version,):
                continue
            messages = []
            for line in data.splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if isinstance(entry, dict) and entry.get('type') in ('user', 'assistant'):
                    message = entry.get('message')
                    if isinstance(message, dict) and 'content' in message:
                        content = message['content']
                        messages.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
            text = self.redactor.redact_text('\n'.join(messages)).text
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO docs VALUES (?, ?, ?)', (ident, version, text))
        return {'result': 'indexed_local', 'external_index': 'disabled'}

    def search(self, query):
        clean = self.redactor.redact_text(query).text.casefold()
        return [{'id': ident, 'version': version, 'text': text}
                for ident, version, text in self.db.execute('SELECT id, version, text FROM docs ORDER BY id')
                if clean in text.casefold()]


def local_run():
    import os
    from pathlib import Path
    source = os.environ.get('AX_CONVERSATION_HISTORY_DIR')
    state = os.environ.get('AX_DATA_DIR')
    if not source or not state:
        return {'result': 'disabled', 'reason': 'history_source_unavailable'}
    if not os.environ.get('AX_REDACT_INDEX') or not os.environ.get('AX_PII_LIST'):
        return {'result': 'disabled', 'reason': 'redaction_unavailable'}
    redactor = from_environment()
    if not redactor.index_ok or not redactor.pii_list_ok:
        return {'result': 'disabled', 'reason': 'redaction_unavailable'}
    directory = Path(state) / 'conversation-index'
    directory.mkdir(parents=True, exist_ok=True)
    index = LocalHistoryIndex(source, directory / 'index.sqlite3', redactor)
    try:
        return index.run()
    finally:
        index.db.close()
