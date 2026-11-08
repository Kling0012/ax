"""Durable local operations and timeline outbox. No destructive cleanup."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Callable

from axcommon.events import build_event


class Journal:
    def __init__(self, path: str | Path, *, scrub: Callable, host: str = "unknown", clock: Callable | None = None):
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        from axcommon.timeutil import now
        self.scrub, self.host = scrub, host
        self.clock = clock or (lambda: now().timestamp())
        with self.db:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state(kind TEXT, key TEXT, body TEXT NOT NULL,
                PRIMARY KEY(kind,key));
            CREATE TABLE IF NOT EXISTS operation(key TEXT PRIMARY KEY, kind TEXT NOT NULL,
                state TEXT NOT NULL, body TEXT NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS event(id TEXT PRIMARY KEY, body TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0);
            """)

    def close(self):
        self.db.close()

    def get(self, kind: str, key: str):
        with self.lock:
            row = self.db.execute("SELECT body FROM state WHERE kind=? AND key=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind: str, key: str, value: dict):
        body = json.dumps(self.scrub(value), sort_keys=True)
        with self.lock, self.db:
            self.db.execute("INSERT INTO state VALUES (?,?,?) ON CONFLICT(kind,key) DO UPDATE SET body=excluded.body", (kind, key, body))

    def claim(self, key: str, kind: str, body: dict, *, retry_failed: bool = False) -> bool:
        """Persist launching before a write. Only one caller wins, including restarts."""
        value = json.dumps(self.scrub({**body, "lease_until": self.clock() + 120}), sort_keys=True)
        with self.lock, self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO operation VALUES (?,?, 'launching',?,NULL)", (key, kind, value))
            if cur.rowcount == 1:
                return True
            if retry_failed:
                cur = self.db.execute("UPDATE operation SET state='launching',body=?,result=NULL WHERE key=? AND kind=? AND state='failed'", (value, key, kind))
                return cur.rowcount == 1
            return False

    def operation(self, key: str):
        with self.lock:
            row = self.db.execute("SELECT * FROM operation WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "body": json.loads(row["body"]), "result": json.loads(row["result"]) if row["result"] else None}

    def finish(self, key: str, state: str, result: dict | None = None):
        if state not in {"launching", "succeeded", "failed", "lost", "retry_wait"}:
            raise ValueError("invalid operation state")
        with self.lock, self.db:
            self.db.execute("UPDATE operation SET state=?,result=? WHERE key=?", (state, json.dumps(self.scrub(result or {})), key))

    def event(self, kind: str, subject: str, data: dict, *, project: str | None = None) -> str:
        ev = build_event(component="change", host=self.host, subloop="change", project=project,
                         kind=kind, subject=subject, data=self.scrub(data))
        with self.lock, self.db:
            self.db.execute("INSERT INTO event(id,body) VALUES (?,?)", (ev["id"], json.dumps(ev)))
        return ev["id"]

    def events(self, *, pending: bool = False):
        with self.lock:
            rows = self.db.execute("SELECT body FROM event" + (" WHERE delivered=0" if pending else "") + " ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]

    def flush(self, send: Callable[[dict], object]) -> int:
        count = 0
        for ev in self.events(pending=True):
            try:
                if send(ev) is False:
                    continue
            except Exception:
                continue
            with self.lock, self.db:
                self.db.execute("UPDATE event SET delivered=1 WHERE id=?", (ev["id"],))
            count += 1
        return count


class Learning:
    def __init__(self, journal: Journal):
        self.journal = journal

    def record(self, candidate: dict) -> bool:
        required = {"id", "source_event", "project", "reason", "evidence_ref", "status"}
        if not required.issubset(candidate) or candidate["status"] != "pending":
            raise ValueError("invalid learning candidate")
        try:
            old = self.journal.get("learning", candidate["id"])
            if old:
                return True
            self.journal.event("change.learning", candidate["id"], candidate, project=candidate["project"])
            self.journal.put("learning", candidate["id"], candidate)
            return True
        except Exception:
            # The originating event is already durable. Its candidate can be
            # reconstructed after storage recovers without stopping the job.
            return False

    def transition(self, candidate_id: str, status: str, *, reason: str) -> dict:
        value = self.journal.get("learning", candidate_id)
        if not value:
            raise KeyError(candidate_id)
        allowed = {"pending": {"selected", "rejected"}, "selected": {"consumed"}, "rejected": set(), "consumed": set()}
        if status not in allowed[value["status"]]:
            raise ValueError("invalid learning transition")
        value = {**value, "status": status, "selection_reason": reason}
        self.journal.event("change.learning", candidate_id, value, project=value["project"])
        self.journal.put("learning", candidate_id, value)
        return value
