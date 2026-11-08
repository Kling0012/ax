"""台帳の保存先（D-2.1 の ledger-data、D-8）。SQLite。1つの接続を lock の下で使う。

表: todos、deps（D-2.1）、todo_history、focus_state（1行）、daily_state（日付が主キー）、
state_history、goals、promises、schedules、generic_history、decisions、timeline_cursor。
過去の内容を消さない（履歴は追記だけ。S-9.4）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
  id TEXT PRIMARY KEY,
  project TEXT, subloop TEXT, title TEXT NOT NULL, state TEXT NOT NULL, owner TEXT NOT NULL,
  reason TEXT, depends_on TEXT NOT NULL DEFAULT '[]', check_at TEXT, source TEXT NOT NULL DEFAULT '{}',
  artifacts TEXT NOT NULL DEFAULT '[]', goal_ids TEXT NOT NULL DEFAULT '[]', promise_ids TEXT NOT NULL DEFAULT '[]',
  due_at TEXT, cancel_reason TEXT, resume_ref TEXT NOT NULL DEFAULT 'null',
  user_only INTEGER NOT NULL DEFAULT 0, card TEXT NOT NULL DEFAULT 'null', item_kind TEXT,
  created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  due_notified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deps (
  todo_id TEXT NOT NULL, depends_on TEXT NOT NULL, PRIMARY KEY (todo_id, depends_on)
);
CREATE INDEX IF NOT EXISTS deps_rev ON deps (depends_on);
CREATE TABLE IF NOT EXISTS todo_history (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, todo_id TEXT NOT NULL, ts TEXT NOT NULL, actor TEXT,
  kind TEXT NOT NULL, before TEXT, after TEXT
);
CREATE TABLE IF NOT EXISTS focus_state (
  id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL, as_of TEXT NOT NULL,
  version INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_state (
  date TEXT PRIMARY KEY, body TEXT NOT NULL, as_of TEXT NOT NULL,
  version INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state_history (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, date TEXT, ts TEXT NOT NULL,
  issuer TEXT NOT NULL, before TEXT, after TEXT
);
CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promises (
  id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open',
  todo_id TEXT NOT NULL, due_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'planned',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS generic_history (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target_id TEXT NOT NULL, ts TEXT NOT NULL,
  actor TEXT, kind_of_change TEXT NOT NULL, before TEXT, after TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, project TEXT, title TEXT NOT NULL, body TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '', basis TEXT NOT NULL, quote TEXT, supersedes TEXT,
  recorded_by TEXT NOT NULL DEFAULT '{}', attested INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active', ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timeline_cursor (
  kind TEXT PRIMARY KEY, after TEXT NOT NULL
);
"""


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _uj(text: str | None, default: Any = None) -> Any:
    if text is None:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


class Store:
    """台帳の SQLite。呼び出し側は service だけ（lock はここで取る）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            if self._tx_depth == 0:
                self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- 汎用 ----
    def execute(self, sql: str, params: tuple | list = ()) -> None:
        with self._lock:
            self._db.execute(sql, params)
            if self._tx_depth == 0:
                self._db.commit()

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def transaction(self) -> "_Tx":
        return _Tx(self)

    # ---- ToDo ----
    def put_todo(self, todo: dict, *, actor: str | None, kind_of_change: str, before: dict | None) -> None:
        cols = {
            "id": todo["id"], "project": todo.get("project"), "subloop": todo.get("subloop"),
            "title": todo["title"], "state": todo["state"], "owner": todo.get("owner", "system"),
            "reason": todo.get("reason"), "depends_on": _j(todo.get("depends_on") or []),
            "check_at": todo.get("check_at"), "source": _j(todo.get("source") or {}),
            "artifacts": _j(todo.get("artifacts") or []), "goal_ids": _j(todo.get("goal_ids") or []),
            "promise_ids": _j(todo.get("promise_ids") or []), "due_at": todo.get("due_at"),
            "cancel_reason": todo.get("cancel_reason"), "resume_ref": _j(todo.get("resume_ref")),
            "user_only": 1 if todo.get("user_only") else 0, "card": _j(todo.get("card")),
            "item_kind": todo.get("item_kind"), "created_by": todo.get("created_by"),
            "created_at": todo.get("created_at"), "updated_at": todo.get("updated_at"),
            "due_notified": 1 if todo.get("due_notified") else 0,
        }
        names = ",".join(cols)
        marks = ",".join("?" for _ in cols)
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO todos ({names}) VALUES ({marks})", tuple(cols.values()))
            self._db.execute(
                "INSERT INTO todo_history (todo_id, ts, actor, kind, before, after) VALUES (?,?,?,?,?,?)",
                (todo["id"], todo["updated_at"], actor, kind_of_change, _j(before) if before else None, _j(todo)))
            if self._tx_depth == 0:
                self._db.commit()

    def get_todo(self, todo_id: str) -> dict | None:
        rows = self.query("SELECT * FROM todos WHERE id=?", (todo_id,))
        return todo_row(rows[0]) if rows else None

    def list_todos(self, where: str, params: list) -> list[dict]:
        return [todo_row(r) for r in self.query(f"SELECT * FROM todos {where} ORDER BY created_at, id", params)]

    def dependents_of(self, todo_id: str) -> list[str]:
        return [r["todo_id"] for r in self.query("SELECT todo_id FROM deps WHERE depends_on=? ORDER BY todo_id", (todo_id,))]

    def replace_deps(self, todo_id: str, depends_on: list[str]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM deps WHERE todo_id=?", (todo_id,))
            for d in depends_on:
                self._db.execute("INSERT OR IGNORE INTO deps (todo_id, depends_on) VALUES (?,?)", (todo_id, d))
            if self._tx_depth == 0:
                self._db.commit()

    def record_work_request(self, todo_id: str, request: dict, *, actor: str, ts: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO todo_history (todo_id, ts, actor, kind, before, after) VALUES (?,?,?,?,?,?)",
                (todo_id, ts, actor, "work.requested_stop", None, _j(request)))
            if self._tx_depth == 0:
                self._db.commit()

    def latest_todo_history_seq(self, todo_id: str) -> int | None:
        rows = self.query("SELECT MAX(seq) AS seq FROM todo_history WHERE todo_id=?", (todo_id,))
        return rows[0]["seq"] if rows else None

    def todo_history(self, todo_id: str) -> list[dict]:
        return [dict(seq=r["seq"], ts=r["ts"], actor=r["actor"], kind=r["kind"],
                     before=_uj(r["before"]), after=_uj(r["after"]))
                for r in self.query("SELECT * FROM todo_history WHERE todo_id=? ORDER BY seq", (todo_id,))]

    # ---- 集中状態・日次状態 ----
    def get_focus(self) -> tuple[dict | None, str | None, int]:
        rows = self.query("SELECT * FROM focus_state WHERE id=1")
        if not rows:
            return None, None, 0
        r = rows[0]
        return _uj(r["body"]), r["as_of"], r["version"]

    def put_focus(self, body: dict, as_of: str, version: int, ts: str) -> None:
        self.execute("INSERT OR REPLACE INTO focus_state (id, body, as_of, version, updated_at) VALUES (1,?,?,?,?)",
                     (_j(body), as_of, version, ts))

    def get_daily(self, date: str) -> tuple[dict | None, str | None, int]:
        rows = self.query("SELECT * FROM daily_state WHERE date=?", (date,))
        if not rows:
            return None, None, 0
        r = rows[0]
        return _uj(r["body"]), r["as_of"], r["version"]

    def put_daily(self, date: str, body: dict, as_of: str, version: int, ts: str) -> None:
        self.execute("INSERT OR REPLACE INTO daily_state (date, body, as_of, version, updated_at) VALUES (?,?,?,?,?)",
                     (date, _j(body), as_of, version, ts))

    def add_state_history(self, kind: str, date: str | None, ts: str, issuer: str, before: dict | None, after: dict) -> None:
        self.execute("INSERT INTO state_history (kind, date, ts, issuer, before, after) VALUES (?,?,?,?,?,?)",
                     (kind, date, ts, issuer, _j(before) if before else None, _j(after)))

    def state_history(self, kind: str, date: str | None = None) -> list[dict]:
        q = "SELECT * FROM state_history WHERE kind=?"
        p: list = [kind]
        if date:
            q += " AND date=?"
            p.append(date)
        return [dict(seq=r["seq"], kind=r["kind"], date=r["date"], ts=r["ts"], issuer=r["issuer"],
                     before=_uj(r["before"]), after=_uj(r["after"]))
                for r in self.query(q + " ORDER BY seq", p)]

    # ---- 目標・約束・予定 ----
    def _item_table(self, kind: str) -> str:
        return {"goal": "goals", "promise": "promises", "schedule": "schedules"}[kind]

    def put_item(self, kind: str, item: dict, *, actor: str | None, kind_of_change: str, before: dict | None) -> None:
        table = self._item_table(kind)
        with self._lock:
            cols = {"id": item["id"], "body": _j(item), "state": item["state"],
                    "created_at": item.get("created_at"), "updated_at": item.get("updated_at")}
            if kind == "promise":
                cols["todo_id"] = item.get("todo_id")
                cols["due_at"] = item.get("due_at")
            names = ",".join(cols)
            marks = ",".join("?" for _ in cols)
            self._db.execute(f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})", tuple(cols.values()))
            self._db.execute(
                "INSERT INTO generic_history (kind, target_id, ts, actor, kind_of_change, before, after) VALUES (?,?,?,?,?,?,?)",
                (kind, item["id"], item.get("updated_at"), actor, kind_of_change,
                 _j(before) if before else None, _j(item)))
            if self._tx_depth == 0:
                self._db.commit()

    def get_item(self, kind: str, item_id: str) -> dict | None:
        table = self._item_table(kind)
        rows = self.query(f"SELECT * FROM {table} WHERE id=?", (item_id,))
        if not rows:
            return None
        item = _uj(rows[0]["body"])
        item["state"] = rows[0]["state"]
        return item

    def list_items(self, kind: str, where: str, params: list) -> list[dict]:
        table = self._item_table(kind)
        out = []
        for r in self.query(f"SELECT * FROM {table} {where} ORDER BY created_at, id", params):
            item = _uj(r["body"])
            item["state"] = r["state"]
            out.append(item)
        return out

    def item_history(self, kind: str, item_id: str) -> list[dict]:
        return [dict(seq=r["seq"], ts=r["ts"], actor=r["actor"], kind_of_change=r["kind_of_change"],
                     before=_uj(r["before"]), after=_uj(r["after"]))
                for r in self.query(
                    "SELECT * FROM generic_history WHERE kind=? AND target_id=? ORDER BY seq", (kind, item_id))]

    # ---- 判断の記録（S-9.6）----
    def put_decision(self, d: dict) -> None:
        self.execute(
            "INSERT OR REPLACE INTO decisions (id, kind, project, title, body, reason, basis, quote, supersedes,"
            " recorded_by, attested, status, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["id"], d["kind"], d.get("project"), d["title"], d["body"], d.get("reason") or "", d["basis"],
             d.get("quote"), d.get("supersedes"), _j(d.get("recorded_by") or {}), 1 if d.get("attested") else 0,
             d.get("status", "active"), d["ts"]))

    def get_decision(self, decision_id: str) -> dict | None:
        rows = self.query("SELECT * FROM decisions WHERE id=?", (decision_id,))
        return decision_row(rows[0]) if rows else None

    def list_decisions(self, where: str, params: list) -> list[dict]:
        return [decision_row(r) for r in
                self.query(f"SELECT * FROM decisions {where} ORDER BY ts DESC, id", params)]

    def supersede_decision(self, decision_id: str) -> None:
        self.execute("UPDATE decisions SET status='superseded' WHERE id=?", (decision_id,))

    # ---- 時系列記録の購読の位置（D-8）----
    def get_cursor(self, kind: str) -> str | None:
        rows = self.query("SELECT after FROM timeline_cursor WHERE kind=?", (kind,))
        return rows[0]["after"] if rows else None

    def set_cursor(self, kind: str, after: str) -> None:
        self.execute("INSERT OR REPLACE INTO timeline_cursor (kind, after) VALUES (?,?)", (kind, after))


def todo_row(r: sqlite3.Row) -> dict:
    todo = {
        "id": r["id"], "project": r["project"], "subloop": r["subloop"], "title": r["title"],
        "state": r["state"], "owner": r["owner"], "reason": r["reason"],
        "depends_on": _uj(r["depends_on"], []), "check_at": r["check_at"], "source": _uj(r["source"], {}),
        "artifacts": _uj(r["artifacts"], []), "goal_ids": _uj(r["goal_ids"], []),
        "promise_ids": _uj(r["promise_ids"], []), "due_at": r["due_at"], "cancel_reason": r["cancel_reason"],
        "resume_ref": _uj(r["resume_ref"]), "user_only": bool(r["user_only"]), "card": _uj(r["card"]),
        "item_kind": r["item_kind"], "created_by": r["created_by"], "created_at": r["created_at"],
        "updated_at": r["updated_at"], "due_notified": bool(r["due_notified"]),
    }
    if todo["card"] is None:
        todo.pop("card")
    if todo["resume_ref"] is None:
        todo.pop("resume_ref")
    return todo


def decision_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"], "kind": r["kind"], "project": r["project"], "title": r["title"], "body": r["body"],
        "reason": r["reason"], "basis": r["basis"], "quote": r["quote"], "supersedes": r["supersedes"],
        "recorded_by": _uj(r["recorded_by"], {}), "attested": bool(r["attested"]), "status": r["status"], "ts": r["ts"],
    }


class _Tx:
    """保存と履歴の追記を1つのトランザクションにする（D-8）。with store.transaction() as tx: tx.store…"""

    def __init__(self, store: Store):
        self.store = store

    def __enter__(self) -> "_Tx":
        self.store._lock.acquire()
        try:
            self.store._db.execute(f"SAVEPOINT ledger_tx_{self.store._tx_depth}")
            self.store._tx_depth += 1
            return self
        except BaseException:
            self.store._lock.release()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.store._tx_depth -= 1
            name = f"ledger_tx_{self.store._tx_depth}"
            if exc_type is not None:
                self.store._db.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.store._db.execute(f"RELEASE SAVEPOINT {name}")
        finally:
            self.store._lock.release()
