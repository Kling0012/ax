"""archiver の状態（SQLite。ボリューム /data の archiver.sqlite）。

transferred: 内容の照合に成功したファイル（同じ大きさ・mtime のファイルを毎回送り直さないため）。
failures: 退避元ごと・理由ごとの失敗（同じ原因は 1 件のカード。S-14.3、F-20）。
削除はしない。ファイルが元から無くなっても行は残す（記録）。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS transferred (
  source TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, inode INTEGER NOT NULL,
  sha256 TEXT NOT NULL, dest TEXT NOT NULL, verified_at TEXT NOT NULL,
  PRIMARY KEY (source, path)
);
CREATE TABLE IF NOT EXISTS failures (
  source TEXT NOT NULL, reason TEXT NOT NULL, first_at TEXT NOT NULL, last_at TEXT NOT NULL, count INTEGER NOT NULL,
  notified_at TEXT, card_state TEXT,
  PRIMARY KEY (source, reason)
);
"""


class ArchiverLedger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        self._c.executescript(SCHEMA)

    def close(self) -> None:
        self._c.close()

    def known(self, source: str, path: str, size: int, mtime_ns: int) -> bool:
        with self._lock:
            row = self._c.execute("SELECT size, mtime_ns FROM transferred WHERE source=? AND path=?", (source, path)).fetchone()
        return bool(row) and row["size"] == size and row["mtime_ns"] == mtime_ns

    def record(self, source: str, states, dest: str, at: str) -> None:
        with self._lock:
            self._c.execute("BEGIN")
            try:
                for s in states:
                    self._c.execute(
                        "INSERT INTO transferred(source, path, size, mtime_ns, inode, sha256, dest, verified_at) VALUES (?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(source, path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, inode=excluded.inode, "
                        "sha256=excluded.sha256, dest=excluded.dest, verified_at=excluded.verified_at",
                        (source, s.path, s.size, s.mtime_ns, s.inode, s.sha256, dest, at))
                self._c.execute("COMMIT")
            except BaseException:
                self._c.execute("ROLLBACK")
                raise

    def count(self, source: str | None = None) -> int:
        with self._lock:
            if source:
                return self._c.execute("SELECT COUNT(*) FROM transferred WHERE source=?", (source,)).fetchone()[0]
            return self._c.execute("SELECT COUNT(*) FROM transferred").fetchone()[0]

    def note_failure(self, source: str, reason: str, at: str) -> dict:
        """失敗を記録する。戻り値 {first: この (source, reason) で初めてか, count}。"""
        with self._lock:
            row = self._c.execute("SELECT count FROM failures WHERE source=? AND reason=?", (source, reason)).fetchone()
            if row is None or row["count"] == 0:
                self._c.execute(
                    "INSERT INTO failures(source, reason, first_at, last_at, count) VALUES (?,?,?,?,1) "
                    "ON CONFLICT(source, reason) DO UPDATE SET first_at=excluded.first_at, last_at=excluded.last_at, count=1, "
                    "notified_at=NULL, card_state=NULL", (source, reason, at, at))
                return {"first": True, "count": 1}
            self._c.execute("UPDATE failures SET last_at=?, count=count+1 WHERE source=? AND reason=?", (at, source, reason))
            return {"first": False, "count": row["count"] + 1}

    def mark_notified(self, source: str, reason: str, at: str, state: str) -> None:
        with self._lock:
            self._c.execute("UPDATE failures SET notified_at=?, card_state=? WHERE source=? AND reason=?", (at, state, source, reason))

    def clear_failures(self, source: str) -> int:
        """成功したら、その退避元の失敗の「現在の状態」を解く（次に同じ理由で失敗したら、新しい 1 件として扱う）。行は残さず notified を外す。"""
        with self._lock:
            cur = self._c.execute("UPDATE failures SET count=0, notified_at=NULL, card_state=NULL, first_at=last_at WHERE source=? AND count>0", (source,))
            return cur.rowcount

    def failure(self, source: str, reason: str) -> dict | None:
        with self._lock:
            row = self._c.execute("SELECT * FROM failures WHERE source=? AND reason=?", (source, reason)).fetchone()
        return dict(row) if row else None
