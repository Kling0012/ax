"""AX-only statistics and proposal queue. No legacy path is accepted or read."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3

from axcommon.ids import new_ulid
from axcommon.timeutil import format_ts, parse_ts

from .contracts import Draft, Target


def _dump_draft(draft: Draft) -> str:
    return json.dumps({"draft_id": draft.draft_id, "text": draft.text, "due_at": draft.due_at,
                       "images": list(draft.images),
                       "artifacts": {k: base64.b64encode(v).decode("ascii") for k, v in draft.artifacts.items()}},
                      ensure_ascii=False)


def restore_draft(row: dict) -> Draft:
    body = json.loads(row["payload"])
    return Draft(body["draft_id"], body["text"], body["due_at"], tuple(body["images"]),
                 {k: base64.b64decode(v, validate=True) for k, v in body["artifacts"].items()})


class QueueStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "x-posts.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, target TEXT NOT NULL, period TEXT NOT NULL,
                    collected_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rejected_proposals (
                    id TEXT PRIMARY KEY, target TEXT NOT NULL, period TEXT NOT NULL,
                    created_at TEXT NOT NULL, draft_id TEXT, text TEXT, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY, plugin_id TEXT NOT NULL, project TEXT NOT NULL,
                    repo TEXT NOT NULL, destination TEXT NOT NULL, period TEXT NOT NULL,
                    draft_id TEXT NOT NULL, created_at TEXT NOT NULL, due_at TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
                    failures INTEGER NOT NULL DEFAULT 0, last_attempt TEXT,
                    sent_at TEXT, external_id TEXT,
                    UNIQUE(plugin_id, project, repo, destination, period, draft_id));
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def single_run(self):
        """One invocation at a time, also across processes and engine instances."""
        fd = os.open(self.directory / "send.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def observation(self, target: Target, period: str, timestamp: str, payload: dict) -> str:
        identifier = "ax-x-stats-" + new_ulid()
        with self.connection() as db:
            db.execute("INSERT INTO observations VALUES (?, ?, ?, ?, ?)",
                       (identifier, json.dumps(target.delivery_target()), period, timestamp,
                        json.dumps(payload, ensure_ascii=False)))
        return identifier

    def read_observation(self, identifier: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM observations WHERE id=?", (identifier,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["target"] = json.loads(result["target"])
            result["payload"] = json.loads(result["payload"])
            return result

    def reject(self, target: Target, period: str, draft: Draft, timestamp: str) -> str:
        identifier = "ax-x-held-" + new_ulid()
        draft_id, text = getattr(draft, "draft_id", None), getattr(draft, "text", None)
        with self.connection() as db:
            db.execute("INSERT INTO rejected_proposals VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (identifier, json.dumps(target.delivery_target()), period, timestamp,
                        draft_id if isinstance(draft_id, str) else None,
                        text if isinstance(text, str) else None, "invalid_proposal"))
        return identifier

    def add(self, target: Target, period: str, draft: Draft, timestamp: str,
            status: str, reason: str = "") -> dict:
        parse_ts(draft.due_at)
        if not isinstance(draft.draft_id, str) or not draft.draft_id.strip():
            raise ValueError("draft_id required")
        identifier = "ax-x-" + new_ulid()
        with self.connection() as db:
            db.execute("""INSERT OR IGNORE INTO candidates
                (id, plugin_id, project, repo, destination, period, draft_id, created_at,
                 due_at, payload, status, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (identifier, target.plugin_id, target.project, target.repo, target.destination,
                        period, draft.draft_id, timestamp, format_ts(parse_ts(draft.due_at)), _dump_draft(draft), status, reason))
            return dict(db.execute("""SELECT * FROM candidates WHERE plugin_id=? AND project=?
                AND repo=? AND destination=? AND period=? AND draft_id=?""",
                                   (target.plugin_id, target.project, target.repo, target.destination,
                                    period, draft.draft_id)).fetchone())

    def get(self, identifier: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM candidates WHERE id=?", (identifier,)).fetchone()
            return dict(row) if row else None

    def candidates(self, target: Target) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("""SELECT * FROM candidates WHERE plugin_id=? AND project=?
                AND repo=? AND destination=? ORDER BY due_at, created_at, id""",
                              (target.plugin_id, target.project, target.repo, target.destination)).fetchall()
            return [dict(row) for row in rows]

    def update(self, identifier: str, **fields):
        allowed = {"status", "reason", "last_attempt", "sent_at", "external_id", "failures"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("invalid queue update")
        with self.connection() as db:
            db.execute("UPDATE candidates SET " + ", ".join(key + "=?" for key in fields) + " WHERE id=?",
                       (*fields.values(), identifier))

    def latest_send(self, destination: str) -> str | None:
        with self.connection() as db:
            return db.execute("SELECT MAX(sent_at) FROM candidates WHERE destination=? AND status='sent'",
                              (destination,)).fetchone()[0]
