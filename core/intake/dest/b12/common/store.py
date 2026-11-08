"""Durable intake delivery and acceptance checkpoints; internal to B12."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path


class Store:
    def __init__(self, path=None):
        self.lock = threading.RLock()
        self.closed = False
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(str(path) if path is not None else ":memory:", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.commit()

    def _require_open_locked(self):
        if self.closed:
            raise RuntimeError("store is closed")

    def get(self, key):
        with self.lock:
            self._require_open_locked()
            row = self.db.execute("SELECT body FROM records WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, key, value):
        with self.lock:
            self._require_open_locked()
            with self.db:
                self.db.execute("INSERT INTO records VALUES (?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body",
                                (key, json.dumps(value, ensure_ascii=False, sort_keys=True)))

    def work_requests(self):
        with self.lock:
            self._require_open_locked()
            return [json.loads(row[0]) for row in self.db.execute(
                "SELECT body FROM records WHERE key LIKE 'read:%' OR key LIKE 'work:%'")]

    def read_requests(self):
        with self.lock:
            self._require_open_locked()
            return [(row[0], json.loads(row[1])) for row in self.db.execute(
                "SELECT key, body FROM records WHERE key LIKE 'read:%'")]

    def items(self):
        with self.lock:
            self._require_open_locked()
            return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM records WHERE key LIKE 'item:%'")]

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.db.close()
