"""通知配送の保存先（HQ の自分のボリューム。SQLite。1 つの接続を lock の下で使う）。

表:
  notifications   通知 1 件 1 行。state は pending（送る順番待ち。next_try_at まで待つ）、held（枠を超えて保持）、sent、
                  blocked（検査で送らない）、failed（再試行の上限）、cancelled（送る前に対象が閉じた）
  question_state  (todo_id, qversion) ごとの最初と最後の「成功通知」の時刻（S-22.3。失敗は数えない）
  actions         通知からの操作の冪等キー（op_id）と結果（T-54）
  digests         朝の要約の対象日ごとの配信状態（同じ対象日を重複配信しない。S-22.5）
過去の行は消さない（状態を変えるだけ）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from functools import wraps
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY, dest TEXT NOT NULL, kind TEXT NOT NULL, todo_id TEXT, qversion INTEGER,
  dedupe_key TEXT, title TEXT NOT NULL, message TEXT NOT NULL, ref TEXT, priority INTEGER NOT NULL DEFAULT 3,
  tags TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  next_try_at TEXT, created_at TEXT NOT NULL, occurred_at TEXT NOT NULL, sent_at TEXT, external_id TEXT, last_error TEXT
);
CREATE INDEX IF NOT EXISTS notif_state ON notifications (dest, state, created_at);
CREATE INDEX IF NOT EXISTS notif_todo ON notifications (todo_id, kind);
CREATE TABLE IF NOT EXISTS delivery_receipts (
  delivery_id TEXT PRIMARY KEY, destination TEXT NOT NULL, payload_hash TEXT NOT NULL,
  state TEXT NOT NULL, delivered_at REAL, next_retry REAL, accepted_at REAL, provider_id TEXT
);
CREATE TABLE IF NOT EXISTS question_state (
  todo_id TEXT NOT NULL, qversion INTEGER NOT NULL, first_success_at TEXT, last_success_at TEXT, successes INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (todo_id, qversion)
);
CREATE TABLE IF NOT EXISTS actions (
  op_id TEXT PRIMARY KEY, todo_id TEXT NOT NULL, action TEXT NOT NULL, ok INTEGER NOT NULL, status INTEGER NOT NULL,
  result TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digests (
  date TEXT PRIMARY KEY, notification_id TEXT, state TEXT NOT NULL, generated_at TEXT NOT NULL
);
"""

COLUMNS = ("id", "dest", "kind", "todo_id", "qversion", "dedupe_key", "title", "message", "ref", "priority", "tags", "state",
           "attempts", "next_try_at", "created_at", "occurred_at", "sent_at", "external_id", "last_error")


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["tags"] = json.loads(d.get("tags") or "[]")
    return d


def serialized(fn):
    """Serialize a logical operation across the HQ request and notification threads."""
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self.store._lock:
            return fn(self, *args, **kwargs)
    return wrapped


class NotifyStore:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(delivery_receipts)")}
            for name, kind in (("accepted_at", "REAL"), ("provider_id", "TEXT")):
                if name not in columns:
                    self._db.execute(f"ALTER TABLE delivery_receipts ADD COLUMN {name} {kind}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def receipt_get(self, delivery_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM delivery_receipts WHERE delivery_id=?", (delivery_id,)).fetchone()
            return dict(row) if row else None

    def receipt_claim(self, delivery_id: str, destination: str, payload_hash: str, now: float) -> tuple[dict, bool]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM delivery_receipts WHERE delivery_id=?", (delivery_id,)).fetchone()
                if row is None:
                    self._db.execute("INSERT INTO delivery_receipts (delivery_id,destination,payload_hash,state) VALUES (?,?,?,'unknown')",
                                     (delivery_id, destination, payload_hash))
                    claimed = True
                elif row['destination'] != destination or row['payload_hash'] != payload_hash:
                    self._db.execute("COMMIT")
                    return dict(row), False
                elif row['state'] == 'retry' and row['next_retry'] <= now:
                    self._db.execute("UPDATE delivery_receipts SET state='unknown', next_retry=NULL WHERE delivery_id=?", (delivery_id,))
                    claimed = True
                else:
                    claimed = False
                row = self._db.execute("SELECT * FROM delivery_receipts WHERE delivery_id=?", (delivery_id,)).fetchone()
                self._db.execute("COMMIT")
                return dict(row), claimed
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def receipt_update(self, delivery_id: str, state: str, *, delivered_at=None, next_retry=None) -> None:
        with self._lock:
            self._db.execute("UPDATE delivery_receipts SET state=?, delivered_at=?, next_retry=? WHERE delivery_id=?",
                             (state, delivered_at, next_retry, delivery_id))

    def receipt_accepted(self, delivery_id: str, accepted_at: float, provider_id: str | None) -> None:
        with self._lock:
            self._db.execute("UPDATE delivery_receipts SET accepted_at=?, provider_id=? WHERE delivery_id=?",
                             (accepted_at, provider_id, delivery_id))

    def receipt_sent_since(self, timestamp: float) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM delivery_receipts WHERE accepted_at>=?", (timestamp,)).fetchone()[0]

    # --- 通知 ---
    def add(self, n: dict) -> None:
        cols = [c for c in COLUMNS if c in n]
        vals = [json.dumps(n[c], ensure_ascii=False) if c == "tags" else n[c] for c in cols]
        with self._lock:
            self._db.execute(f"INSERT INTO notifications ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)

    def get(self, nid: str) -> dict | None:
        with self._lock:
            return _row(self._db.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone())

    def update(self, nid: str, **fields: Any) -> None:
        if not fields:
            return
        with self._lock:
            self._db.execute(f"UPDATE notifications SET {','.join(k + '=?' for k in fields)} WHERE id=?", [*fields.values(), nid])

    def list(self, *, dest: str | None = None, state: str | None = None, todo_id: str | None = None, kind: str | None = None,
             limit: int = 200) -> list[dict]:
        where, params = [], []
        for col, v in (("dest", dest), ("state", state), ("todo_id", todo_id), ("kind", kind)):
            if v is not None:
                where.append(f"{col}=?")
                params.append(v)
        sql = "SELECT * FROM notifications" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at, id LIMIT ?"
        with self._lock:
            return [_row(r) for r in self._db.execute(sql, [*params, limit]).fetchall()]

    def deliverable(self, dest: str, now_ts: str, limit: int = 200) -> list[dict]:
        """今送ってよい通知（pending で next_try_at が来た物、held）。作った順。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM notifications WHERE dest=? AND (state='held' OR (state='pending' AND (next_try_at IS NULL OR next_try_at<=?))) "
                "ORDER BY created_at, id LIMIT ?", (dest, now_ts, limit)).fetchall()
        return [_row(r) for r in rows]

    def sent_since(self, dest: str, since_ts: str) -> int:
        with self._lock:
            count = self._db.execute("SELECT COUNT(*) FROM notifications WHERE dest=? AND state='sent' AND sent_at>=?",
                                     (dest, since_ts)).fetchone()[0]
            if dest == "ntfy":
                count += self._db.execute("SELECT COUNT(*) FROM delivery_receipts WHERE accepted_at>=strftime('%s', ?)", (since_ts,)).fetchone()[0]
            return count

    def has_open(self, dedupe_key: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM notifications WHERE dedupe_key=? AND state IN ('pending','held') LIMIT 1", (dedupe_key,)).fetchone() is not None

    def by_dedupe(self, dedupe_key: str) -> dict | None:
        with self._lock:
            return _row(self._db.execute(
                "SELECT * FROM notifications WHERE dedupe_key=? ORDER BY created_at,id LIMIT 1", (dedupe_key,)).fetchone())

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {r[0]: r[1] for r in self._db.execute("SELECT state, COUNT(*) FROM notifications GROUP BY state")}

    # --- 質問の通知キー ---
    def question_success(self, todo_id: str, qversion: int, ts: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO question_state (todo_id,qversion,first_success_at,last_success_at,successes) VALUES (?,?,?,?,1) "
                "ON CONFLICT(todo_id,qversion) DO UPDATE SET last_success_at=excluded.last_success_at, successes=successes+1",
                (todo_id, qversion, ts, ts))

    def question_last_success(self, todo_id: str, qversion: int) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT last_success_at FROM question_state WHERE todo_id=? AND qversion=?", (todo_id, qversion)).fetchone()
        return r[0] if r else None

    # --- 操作の冪等キー ---
    def action_get(self, op_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM actions WHERE op_id=?", (op_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["result"] = json.loads(d["result"])
        d["ok"] = bool(d["ok"])
        return d

    def action_put(self, op_id: str, todo_id: str, action: str, ok: bool, status: int, result: dict, ts: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO actions (op_id,todo_id,action,ok,status,result,created_at) VALUES (?,?,?,?,?,?,?)",
                             (op_id, todo_id, action, 1 if ok else 0, status, json.dumps(result, ensure_ascii=False), ts))

    # --- 朝の要約 ---
    def digest_get(self, date: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM digests WHERE date=?", (date,)).fetchone()
        return dict(r) if r else None

    def digest_put(self, date: str, notification_id: str | None, state: str, ts: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO digests (date,notification_id,state,generated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET notification_id=excluded.notification_id, state=excluded.state, generated_at=excluded.generated_at",
                (date, notification_id, state, ts))
