"""D-5.1 approvals table, with atomic single-use transitions."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading


class ApprovalError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def payload_digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ApprovalStore:
    """May share the intake SQLite file; no independent production path default."""
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY, destination TEXT NOT NULL, payload_json TEXT NOT NULL,
            digest TEXT NOT NULL, requested_by TEXT NOT NULL, session TEXT, project TEXT,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL,
            granted_at TEXT, executed_at TEXT, result_json TEXT NOT NULL)""")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def get(self, approval_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise ApprovalError(404, "approval_not_found")
        return dict(row)

    def list(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM approvals ORDER BY created_at,approval_id")]

    def insert(self, row: dict) -> None:
        keys = tuple(row)
        self.db.execute(f"INSERT INTO approvals ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                        tuple(row[k] for k in keys))

    def update(self, approval_id: str, **values) -> None:
        allowed = {"state", "granted_at", "executed_at", "result_json"}
        if not values or not set(values) <= allowed:
            raise ValueError("immutable approval content")
        self.db.execute("UPDATE approvals SET " + ",".join(f"{k}=?" for k in values) + " WHERE approval_id=?",
                        (*values.values(), approval_id))

    def close(self) -> None:
        with self.lock:
            self.db.close()
