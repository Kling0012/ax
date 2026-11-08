"""管理役の永続状態（manager-data。D-2.1「購読のカーソル、判断の記録」）。SQLite。

- inbox: 購読で受け取った契機の事象。仕様は「受信箱（記憶上の列）」だが、再起動で取りこぼさないよう SQLite に置く（DECISIONS）。
  state: new（未処理）／handled（判断して実行した）／ejected（台帳へ出した）／parked（台帳で保留。送れない区分など）。
- meta: cursor_seq（時系列記録の読み位置）、last_tick（最後の時計の周回）、parked_seen（見直した parked の印）。
- decisions: 判断の記録（要約。全文は入れない）。
削除しない（S-2.2 の流儀）。同じ事象 ID は 1 行だけ。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
  event_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, kind TEXT NOT NULL, ts TEXT NOT NULL, event TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'new', attempts INTEGER NOT NULL DEFAULT 0, reason TEXT, cycle_id TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS inbox_state ON inbox(state, seq);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL, step TEXT NOT NULL, ts TEXT NOT NULL, job_id TEXT, model TEXT, doc TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- meta ----
    def meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))

    @property
    def cursor(self) -> int:
        return int(self.meta("cursor_seq", "0") or 0)

    def set_cursor(self, seq: int) -> None:
        if seq > self.cursor:
            self.set_meta("cursor_seq", str(seq))

    # ---- inbox ----
    def add_event(self, ev: dict) -> bool:
        """受信箱に足す。既にあれば False（同じ事象を 2 回扱わない）。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO inbox(event_id, seq, kind, ts, event, state, updated_at) VALUES(?,?,?,?,?, 'new', ?)",
                (ev["id"], int(ev.get("seq") or 0), ev["kind"], ev["ts"], json.dumps(ev, ensure_ascii=False), ev["ts"]))
            return cur.rowcount > 0

    def pending(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT event, attempts FROM inbox WHERE state='new' ORDER BY seq LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            ev = json.loads(r["event"])
            ev["_attempts"] = r["attempts"]
            out.append(ev)
        return out

    def event(self, event_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT event FROM inbox WHERE event_id=?", (event_id,)).fetchone()
        return json.loads(row["event"]) if row else None

    def pending_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM inbox WHERE state='new'").fetchone()[0]

    def mark(self, event_ids: list[str], state: str, *, reason: str | None = None, cycle_id: str | None = None, now: str | None = None) -> None:
        if not event_ids:
            return
        with self._lock:
            self._conn.executemany("UPDATE inbox SET state=?, reason=?, cycle_id=?, updated_at=COALESCE(?, updated_at) WHERE event_id=?",
                                   [(state, reason, cycle_id, now, i) for i in event_ids])

    def bump_attempts(self, event_ids: list[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        with self._lock:
            for i in event_ids:
                self._conn.execute("UPDATE inbox SET attempts=attempts+1 WHERE event_id=?", (i,))
                row = self._conn.execute("SELECT attempts FROM inbox WHERE event_id=?", (i,)).fetchone()
                out[i] = row["attempts"] if row else 0
        return out

    def state_of(self, event_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT state FROM inbox WHERE event_id=?", (event_id,)).fetchone()
        return row["state"] if row else None

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT state, COUNT(*) c FROM inbox GROUP BY state").fetchall()
        return {r["state"]: r["c"] for r in rows}

    # ---- parked の見直しの印 ----
    def parked_seen(self, todo_id: str, check_at: str) -> bool:
        return self.meta(f"parked_seen:{todo_id}") == check_at

    def mark_parked_seen(self, todo_id: str, check_at: str) -> None:
        self.set_meta(f"parked_seen:{todo_id}", check_at)

    # ---- 判断の記録 ----
    def record_decision(self, cycle_id: str, step: str, ts: str, doc: dict, *, job_id: str | None = None, model: str | None = None) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO decisions(cycle_id, step, ts, job_id, model, doc) VALUES(?,?,?,?,?,?)",
                               (cycle_id, step, ts, job_id, model, json.dumps(doc, ensure_ascii=False)))

    def decisions(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**{k: r[k] for k in ("cycle_id", "step", "ts", "job_id", "model")}, "doc": json.loads(r["doc"])} for r in rows]
