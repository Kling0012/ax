"""制御 API の保存先（D-2.1 の control-data、D-4）。SQLite。

表: plugins(id, manifest JSON, revision)（D-2.1）、commands(... renewals)（D-2.1）、timers(...)、permissions(...)。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS effective_limits_sync (
  singleton INTEGER PRIMARY KEY CHECK (singleton=1), revision INTEGER NOT NULL,
  status TEXT NOT NULL, error TEXT, updated_at TEXT
);
INSERT OR IGNORE INTO effective_limits_sync VALUES (1, 0, 'pending', NULL, NULL);
CREATE TABLE IF NOT EXISTS plugins (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL, manifest TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'registered', note TEXT,
  user_set_knobs TEXT NOT NULL DEFAULT '{}', registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
  id TEXT PRIMARY KEY, target TEXT NOT NULL, op TEXT NOT NULL, args TEXT NOT NULL,
  issued_by TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT, on_expiry TEXT NOT NULL DEFAULT 'ask_issuer',
  state TEXT NOT NULL DEFAULT 'active', renewals INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL,
  corr TEXT, released_by TEXT, released_reason TEXT
);
CREATE TABLE IF NOT EXISTS command_run_targets (
  command_id TEXT NOT NULL, plugin_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (command_id, plugin_id)
);
CREATE TABLE IF NOT EXISTS timers (
  id TEXT PRIMARY KEY, at TEXT, every_s INTEGER, action TEXT NOT NULL, reason TEXT NOT NULL,
  target TEXT, command_id TEXT, issued_by TEXT NOT NULL, created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active', fired_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS permissions (
  id TEXT PRIMARY KEY, project TEXT NOT NULL, repo TEXT NOT NULL, destination TEXT NOT NULL,
  operations TEXT NOT NULL, conditions TEXT NOT NULL DEFAULT '{}', source_instruction TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, branch_cleanup TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, created_by TEXT NOT NULL
);
"""


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _uj(t, default=None):
    if t is None:
        return default
    try:
        return json.loads(t)
    except ValueError:
        return default


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def execute(self, sql: str, params: tuple | list = ()) -> None:
        with self._lock:
            self._db.execute(sql, params)
            self._db.commit()

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def next_limits_revision(self, remote_revision: int = 0) -> int:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT revision FROM effective_limits_sync WHERE singleton=1").fetchone()
                revision = max(row[0], remote_revision) + 1
                self._db.execute("UPDATE effective_limits_sync SET revision=?, status='pending', error=NULL WHERE singleton=1", (revision,))
                self._db.commit()
                return revision
            except BaseException:
                self._db.rollback()
                raise

    def limits_sync_result(self, status: str, error: str | None, updated_at: str) -> None:
        self.execute("UPDATE effective_limits_sync SET status=?, error=?, updated_at=? WHERE singleton=1",
                     (status, error, updated_at))

    def limits_sync_status(self) -> dict:
        return dict(self.query("SELECT revision, status, error, updated_at FROM effective_limits_sync WHERE singleton=1")[0])

    # ---- plugins ----
    def upsert_plugin(self, p: dict) -> None:
        self.execute(
            "INSERT OR REPLACE INTO plugins (id, kind, path, manifest, revision, status, note, user_set_knobs, registered_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (p["id"], p["kind"], p["path"], _j(p["manifest"]), p.get("revision", 1), p.get("status", "registered"),
             p.get("note"), _j(p.get("user_set_knobs") or {}), p.get("registered_at")))

    def get_plugin(self, plugin_id: str) -> dict | None:
        rows = self.query("SELECT * FROM plugins WHERE id=?", (plugin_id,))
        return plugin_row(rows[0]) if rows else None

    def list_plugins(self) -> list[dict]:
        return [plugin_row(r) for r in self.query("SELECT * FROM plugins ORDER BY id")]

    def bump_revision(self, plugin_id: str) -> int:
        with self._lock:
            cur = self._db.execute("UPDATE plugins SET revision=revision+1 WHERE id=?", (plugin_id,))
            self._db.commit()
            if cur.rowcount == 0:
                return 0
            return self.query("SELECT revision FROM plugins WHERE id=?", (plugin_id,))[0]["revision"]

    # ---- commands ----
    def put_command(self, c: dict, run_targets: list[str] | None = None) -> None:
        with self._lock:
            try:
                self._db.execute(
                    "INSERT OR REPLACE INTO commands (id, target, op, args, issued_by, issued_at, expires_at, on_expiry,"
                    " state, renewals, reason, corr, released_by, released_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (c["id"], c["target"], c["op"], _j(c["args"]), c["issued_by"], c["issued_at"], c.get("expires_at"),
                     c.get("on_expiry", "ask_issuer"), c.get("state", "active"), c.get("renewals", 0), c["reason"],
                     c.get("corr"), c.get("released_by"), c.get("released_reason")))
                if run_targets is not None:
                    self._db.executemany("INSERT INTO command_run_targets (command_id, plugin_id) VALUES (?, ?)",
                                         [(c["id"], target) for target in run_targets])
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def run_target_in_scope(self, command_id: str, plugin_id: str) -> bool:
        return bool(self.query("SELECT 1 FROM command_run_targets WHERE command_id=? AND plugin_id=? AND active=1",
                               (command_id, plugin_id)))

    def invalidate_run_target(self, plugin_id: str) -> None:
        self.execute("UPDATE command_run_targets SET active=0 WHERE plugin_id=?", (plugin_id,))

    def get_command(self, command_id: str) -> dict | None:
        rows = self.query("SELECT * FROM commands WHERE id=?", (command_id,))
        return command_row(rows[0]) if rows else None

    def list_commands(self, where: str, params: list) -> list[dict]:
        return [command_row(r) for r in self.query(f"SELECT rowid AS seq, * FROM commands {where} ORDER BY issued_at, rowid", params)]

    def living_commands(self) -> list[dict]:
        return [command_row(r) for r in self.query(
            "SELECT rowid AS seq, * FROM commands WHERE state IN ('active','expired_pending') ORDER BY issued_at, rowid")]

    # ---- timers ----
    def put_timer(self, t: dict) -> None:
        self.execute(
            "INSERT OR REPLACE INTO timers (id, at, every_s, action, reason, target, command_id, issued_by,"
            " created_at, expires_at, state, fired_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["id"], t.get("at"), t.get("every_s"), t["action"], t["reason"], t.get("target"), t.get("command_id"),
             t["issued_by"], t["created_at"], t["expires_at"], t.get("state", "active"), t.get("fired_count", 0)))

    def get_timer(self, timer_id: str) -> dict | None:
        rows = self.query("SELECT * FROM timers WHERE id=?", (timer_id,))
        return timer_row(rows[0]) if rows else None

    def list_timers(self, state: str | None = None) -> list[dict]:
        if state:
            return [timer_row(r) for r in self.query("SELECT * FROM timers WHERE state=? ORDER BY id", (state,))]
        return [timer_row(r) for r in self.query("SELECT * FROM timers ORDER BY id")]

    # ---- permissions ----
    def put_permission(self, p: dict) -> None:
        self.execute(
            "INSERT OR REPLACE INTO permissions (id, project, repo, destination, operations, conditions,"
            " source_instruction, enabled, branch_cleanup, created_at, updated_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["id"], p["project"], p["repo"], p["destination"], _j(p["operations"]), _j(p.get("conditions") or {}),
             p["source_instruction"], 1 if p.get("enabled", True) else 0, p.get("branch_cleanup"),
             p["created_at"], p["updated_at"], p.get("created_by", "")))

    def get_permission(self, permission_id: str) -> dict | None:
        rows = self.query("SELECT * FROM permissions WHERE id=?", (permission_id,))
        return permission_row(rows[0]) if rows else None

    def list_permissions(self) -> list[dict]:
        return [permission_row(r) for r in self.query("SELECT * FROM permissions ORDER BY id")]


def plugin_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "kind": r["kind"], "path": r["path"], "manifest": _uj(r["manifest"], {}),
            "revision": r["revision"], "status": r["status"], "note": r["note"],
            "user_set_knobs": _uj(r["user_set_knobs"], {}), "registered_at": r["registered_at"]}


def command_row(r: sqlite3.Row) -> dict:
    out = {"id": r["id"], "target": r["target"], "op": r["op"], "args": _uj(r["args"], {}), "issued_by": r["issued_by"],
            "issued_at": r["issued_at"], "expires_at": r["expires_at"], "on_expiry": r["on_expiry"],
            "state": r["state"], "renewals": r["renewals"], "reason": r["reason"], "corr": r["corr"],
            "released_by": r["released_by"], "released_reason": r["released_reason"]}
    try:
        out["seq"] = r["seq"]
    except (IndexError, KeyError):
        pass
    return out


def timer_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "at": r["at"], "every_s": r["every_s"], "action": r["action"], "reason": r["reason"],
            "target": r["target"], "command_id": r["command_id"], "issued_by": r["issued_by"],
            "created_at": r["created_at"], "expires_at": r["expires_at"], "state": r["state"],
            "fired_count": r["fired_count"]}


def permission_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "project": r["project"], "repo": r["repo"], "destination": r["destination"],
            "operations": _uj(r["operations"], []), "conditions": _uj(r["conditions"], {}),
            "source_instruction": r["source_instruction"], "enabled": bool(r["enabled"]),
            "branch_cleanup": r["branch_cleanup"], "created_at": r["created_at"], "updated_at": r["updated_at"],
            "created_by": r["created_by"]}
