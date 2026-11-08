"""AX-local history, patrol exclusion, and crash recovery (S-23.2 / S-23.7)."""
from __future__ import annotations

import csv
import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from axcommon import timeutil
from core.intake.dest.b16b.delivery import DeliveryJournal


class State:
    def __init__(self, directory, registry, *, emit, lost, plugin_id=None):
        self.plugin_id = plugin_id or os.environ.get("AX_SCHEDULED_PUBLISHER_ID", "scheduled-publisher")
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "patrol.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS candidates (
                  destination TEXT, source_item_id TEXT, payload TEXT, attempted_at TEXT,
                  owner TEXT, PRIMARY KEY(destination,source_item_id));
                CREATE TABLE IF NOT EXISTS runs (
                  destination TEXT, scheduled_at TEXT, run_id TEXT, result TEXT,
                  PRIMARY KEY(destination,scheduled_at));
                CREATE TABLE IF NOT EXISTS search_seen (item_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS owners (env_id TEXT PRIMARY KEY, registered_at TEXT);
                CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER);
            """)
        self.journal = DeliveryJournal(self.root / "delivery.sqlite3", registry, emit=emit, lost=lost)

    def close(self):
        self.journal.close()
        self.db.close()

    @contextmanager
    def single_run(self):
        with (self.root / "patrol.lock").open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def register_owner(self, env_id, *, user_confirmed=False):
        if not user_confirmed:
            raise PermissionError("identity owner registration requires a user action")
        if not isinstance(env_id, str) or not env_id.strip():
            raise ValueError("env_id required")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO owners VALUES (?, ?)", (env_id, timeutil.now_ts()))

    def owner_registered(self, env_id):
        return self.db.execute("SELECT 1 FROM owners WHERE env_id=?", (env_id,)).fetchone() is not None

    def started(self, destination, scheduled_at, run_id):
        with self.db:
            row = self.db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?, 'running')",
                                  (destination, scheduled_at, run_id))
        return row.rowcount == 1

    def finish(self, destination, scheduled_at, result):
        with self.db:
            self.db.execute("UPDATE runs SET result=? WHERE destination=? AND scheduled_at=?",
                            (result, destination, scheduled_at))

    def remember(self, destination, source_id, candidate, now, owner):
        with self.db:
            self.db.execute("INSERT INTO candidates VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(destination,source_item_id) DO UPDATE SET "
                            "attempted_at=excluded.attempted_at,owner=excluded.owner",
                            (destination, source_id, json.dumps(candidate.payload()), now, owner))

    def history(self):
        out = []
        for row in self.db.execute("SELECT * FROM candidates"):
            row = dict(row)
            record = self.journal.get(self.plugin_id, row["destination"], row["source_item_id"])
            if record:
                out.append({**row, **record, "payload": json.loads(row["payload"])})
        return out

    def seen(self):
        return [row[0] for row in self.db.execute("SELECT item_id FROM search_seen")]

    def mark_seen(self, identifiers):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO search_seen VALUES (?)", ((x,) for x in identifiers))

    def failure_streak(self, failed):
        with self.db:
            self.db.execute("INSERT INTO counters VALUES ('failures', ?) ON CONFLICT(name) DO UPDATE "
                            "SET value=CASE WHEN ? THEN value+1 ELSE 0 END", (int(failed), failed))
        return self.db.execute("SELECT value FROM counters WHERE name='failures'").fetchone()[0]

    def export_tsv(self, redactor):
        """The journal is authoritative; regenerate this non-authoritative legacy-shaped view."""
        from datetime import timezone, timedelta
        import hashlib

        with (self.root / "sent.tsv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            writer.writerow(("jst_date", "utc_iso", "action", "status", "target", "sha16", "reason", "preview"))
            for record in self.history():
                payload = record["payload"]
                body = payload["note"] if payload["action"] == "friend_request" else payload["text"]
                timestamp = timeutil.parse_ts(record["checked_at"])
                values = (timestamp.astimezone(timezone(timedelta(hours=9))).date().isoformat(),
                          record["checked_at"], record["operation"], record["state"],
                          payload["conv_id"] or payload["item_id"] or payload["target"],
                          hashlib.sha256(body.encode()).hexdigest()[:16], record["reason"], body[:120])
                writer.writerow([redactor.redact_text(str(value)).text.replace("\n", " ").replace("\r", " ")
                                 for value in values])
