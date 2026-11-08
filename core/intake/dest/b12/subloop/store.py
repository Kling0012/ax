"""Private durable pending table and day admission accounting (D-5)."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .policy import Rejected


class Store:
    def __init__(self, path: str | Path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("CREATE TABLE IF NOT EXISTS subloop_requests "
                        "(request_id TEXT PRIMARY KEY, day TEXT NOT NULL, hq INTEGER NOT NULL, record TEXT NOT NULL)")

    def get(self, request_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT record FROM subloop_requests WHERE request_id=?", (request_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def admit(self, record: dict, *, day: str, hq: bool, daily_max: int) -> dict:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                prior = self.get(record["request_id"])
                if prior:
                    self.db.execute("COMMIT")
                    return prior
                used = self.db.execute("SELECT COUNT(*) FROM subloop_requests WHERE day=? AND hq=0", (day,)).fetchone()[0]
                if not hq and used >= daily_max:
                    raise Rejected(429, "subloop_limit")
                # A parked candidate can be resubmitted from HQ using the same branch.
                active = [json.loads(row[0]) for row in self.db.execute("SELECT record FROM subloop_requests")]
                if any(r["plugin_id"] == record["plugin_id"] and r["phase"] not in {"parked", "registered"} for r in active):
                    raise Rejected(409, "subloop_pending")
                self.db.execute("INSERT INTO subloop_requests VALUES(?,?,?,?)",
                                (record["request_id"], day, int(hq), json.dumps(record)))
                self.db.execute("COMMIT")
                return record
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def save(self, record: dict) -> None:
        with self.lock:
            self.db.execute("UPDATE subloop_requests SET record=? WHERE request_id=?",
                            (json.dumps(record), record["request_id"]))

    def for_todo(self, todo_id: str) -> dict | None:
        with self.lock:
            for row in self.db.execute("SELECT record FROM subloop_requests ORDER BY rowid DESC"):
                record = json.loads(row[0])
                if record.get("todo_id") == todo_id:
                    return record
        return None

    def close(self) -> None:
        self.db.close()
