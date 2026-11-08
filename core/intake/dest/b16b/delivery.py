"""S-23.2 の永続化と送信前の検査。外部の操作は注入した関数だけが行う。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from axcommon import schemas, timeutil
from core.intake.dest.b16b.common import canonical_plugin_id


class NotSent(Exception):
    """外部操作が確実に行われなかった。再試行してよい。"""


class DeliveryJournal:
    def __init__(self, path, registry, *, emit, lost):
        self.registry, self.emit, self.lost = registry, emit, lost
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                plugin_id TEXT NOT NULL, destination TEXT NOT NULL, source_item_id TEXT NOT NULL,
                operation TEXT NOT NULL, state TEXT NOT NULL, external_id TEXT,
                checked_at TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(plugin_id, destination, source_item_id))""")
            # Normalize stored keys atomically; a collision stops this journal
            # rather than risking a duplicate outward delivery.
            plugin_ids = [row[0] for row in self.db.execute("SELECT DISTINCT plugin_id FROM deliveries")]
            for plugin_id in plugin_ids:
                canonical = canonical_plugin_id(plugin_id)
                if canonical != plugin_id:
                    self.db.execute("UPDATE deliveries SET plugin_id=? WHERE plugin_id=?", (canonical, plugin_id))

    def close(self):
        self.db.close()

    def get(self, plugin_id, destination, source_item_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM deliveries WHERE plugin_id=? AND destination=? AND source_item_id=?",
                                  (plugin_id, destination, source_item_id)).fetchone()
            return dict(row) if row else None

    def _notify(self, record):
        schemas.validate("ext/b16b/automation-delivery", record)
        try:
            self.emit("automation.delivery", record["plugin_id"], dict(record))
        except Exception:
            pass  # 永続記録は残す。照合まで再送しない。
        if record["state"] == "unknown":
            try:
                self.lost({**record, "status": "lost"})
            except Exception:
                pass  # report_unknown で再送できる。外部の送信は再試行しない。

    def _finish(self, key, state, external_id=None, reason=""):
        with self.lock, self.db:
            self.db.execute("UPDATE deliveries SET state=?, external_id=?, checked_at=?, reason=? "
                            "WHERE plugin_id=? AND destination=? AND source_item_id=?",
                            (state, external_id, timeutil.now_ts(), reason, *key))
        record = self.get(*key)
        self._notify(record)
        return record

    def recover(self):
        """再起動時、送信処理が止まった後だけ呼ぶ。生きた送信と並行しない。"""
        with self.lock:
            keys = [tuple(row) for row in self.db.execute(
                "SELECT plugin_id,destination,source_item_id FROM deliveries WHERE state='launching'")]
        for key in keys:
            self._finish(key, "unknown", reason="restart_during_send")
        return len(keys)

    def report_unknown(self):
        with self.lock:
            records = [dict(row) for row in self.db.execute("SELECT * FROM deliveries WHERE state='unknown'")]
        for record in records:
            self._notify(record)

    def deliver(self, *, mode, target, operation, source_item_id, registered,
                expected_destination, permission_reader, preflight, send):
        if mode != "run":
            return {"state": "skipped", "reason": "collect_only" if mode == "collect_only" else "mode_blocked"}
        if (not registered or not isinstance(source_item_id, str) or not source_item_id.strip()
                or expected_destination != target.get("destination")
                or not self.registry.contains(target, operation)):
            try:
                self.emit("permission.checked", target.get("repo", "-"),
                          {"permission_id": None, "operation": operation, "result": "denied"})
            except Exception:
                pass
            return {"state": "denied", "reason": "target_not_listed"}
        try:
            allowed = permission_reader.check(target, operation) is True
        except Exception:
            return {"state": "denied", "reason": "permission_unavailable"}
        if not allowed:
            return {"state": "denied", "reason": "permission_denied"}
        try:
            if preflight() is not True:
                return {"state": "denied", "reason": "preflight_failed"}
        except Exception:
            return {"state": "denied", "reason": "preflight_unavailable"}
        key = (target["plugin_id"], target["destination"], source_item_id)
        with self.lock, self.db:
            cursor = self.db.execute("""INSERT INTO deliveries VALUES (?, ?, ?, ?, 'launching', NULL, ?, '')
                ON CONFLICT(plugin_id, destination, source_item_id) DO UPDATE
                SET state='launching', external_id=NULL, checked_at=excluded.checked_at, reason=''
                WHERE deliveries.state='failed' AND deliveries.operation=excluded.operation""",
                (*key, operation, timeutil.now_ts()))
            claimed = cursor.rowcount == 1
        if not claimed:
            return self.get(*key)
        self._notify(self.get(*key))
        try:
            external_id = send()
        except NotSent:
            return self._finish(key, "failed", reason="confirmed_not_sent")
        except Exception:
            return self._finish(key, "unknown", reason="send_outcome_unknown")
        if not isinstance(external_id, str) or not external_id.strip():
            return self._finish(key, "unknown", reason="receipt_missing")
        return self._finish(key, "sent", external_id=external_id)
