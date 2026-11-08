"""Performance improvement entry point with a per-target explicit resume gate."""
from __future__ import annotations

import json
import sqlite3
from typing import Protocol


class Backend(Protocol):
    def prepare(self, target: str, item: str) -> dict: ...
    def create_pr(self, target: str, item: str, change: dict) -> str: ...


class Performance:
    def __init__(self, database: str, backend: Backend):
        self.db = sqlite3.connect(database)
        self.db.execute('CREATE TABLE IF NOT EXISTS targets (target TEXT PRIMARY KEY, resumed INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS changes (key TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT)')
        # An interrupted external operation has an unknown result and is never retried blindly.
        with self.db:
            self.db.execute("UPDATE changes SET status='unknown' WHERE status='launching'")
        self.backend = backend

    def resume(self, target: str, *, issuer: str, explicit: bool) -> bool:
        """The integration must supply an authenticated issuer and validated instruction."""
        if issuer != 'user' or explicit is not True or not isinstance(target, str) or not target.strip() or target == 'all':
            return False
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO targets VALUES (?, 1)', (target,))
        return True

    def stop(self, target: str):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO targets VALUES (?, 0)', (target,))

    def mode(self, target: str) -> str:
        row = self.db.execute('SELECT resumed FROM targets WHERE target = ?', (target,)).fetchone()
        return 'run' if row and row[0] == 1 else 'draining'

    def run(self, target: str, item: str, *, trigger: str, mode: str = 'run') -> dict:
        if trigger not in ('schedule', 'run_now', 'manager', 'inspection', 'emergency'):
            raise ValueError('invalid trigger')
        if self.mode(target) != 'run' or mode != 'run':
            return {'status': 'stopped'}
        try:
            change = self.backend.prepare(target, item)
            if not isinstance(change, dict) or not isinstance(change.get('id'), str) or not change['id']:
                raise ValueError('invalid change')
        except Exception as exc:
            return {'status': 'failed', 'reason': type(exc).__name__}
        key = json.dumps([target, item, change['id']], ensure_ascii=True, separators=(',', ':'))
        row = self.db.execute('SELECT status, result FROM changes WHERE key = ?', (key,)).fetchone()
        if row:
            return {'status': row[0], 'result': row[1]}
        if self.mode(target) != 'run':
            return {'status': 'stopped'}
        with self.db:
            self.db.execute('INSERT INTO changes VALUES (?, ?, NULL)', (key, 'launching'))
        try:
            result = self.backend.create_pr(target, item, change)
            if not isinstance(result, str) or not result:
                raise ValueError('invalid PR result')
        except Exception:
            with self.db:
                self.db.execute("UPDATE changes SET status='unknown' WHERE key=?", (key,))
            return {'status': 'unknown'}
        with self.db:
            self.db.execute("UPDATE changes SET status='sent', result=? WHERE key=?", (result, key))
        return {'status': 'sent', 'result': result}
