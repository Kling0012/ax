"""サブループの保存先（sqlite3。ボリューム `sl-<id>-data` の `subloop.db`。D-6.2）。

表: runs、items、node_runs、edge_counts、inbox、meta。再起動しても残る。削除しない（終わった物は state で区別する）。
単一プロセス・単一の asyncio ループから使う前提で、接続は 1 つ、操作は lock で直列にする。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from axcommon import timeutil

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT, planned_at TEXT, started_at TEXT, ended_at TEXT, trigger_kind TEXT);
CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY, run_id TEXT, state TEXT NOT NULL, reason TEXT, data TEXT NOT NULL, todo_id TEXT,
  depends_on TEXT NOT NULL DEFAULT '[]', hops INTEGER NOT NULL DEFAULT 0, current_node TEXT, started INTEGER NOT NULL DEFAULT 0,
  artifacts TEXT NOT NULL DEFAULT '{}', created_at TEXT, updated_at TEXT, ejected_at TEXT);
CREATE TABLE IF NOT EXISTS node_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, item_id TEXT NOT NULL, node_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  continuation INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, status TEXT, out TEXT, error TEXT, launching_key TEXT, external_ref TEXT,
  started_at TEXT, ended_at TEXT);
CREATE INDEX IF NOT EXISTS node_runs_item ON node_runs(item_id, node_id);
CREATE INDEX IF NOT EXISTS node_runs_state ON node_runs(state);
CREATE TABLE IF NOT EXISTS edge_counts (item_id TEXT NOT NULL, edge_key TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(item_id, edge_key));
CREATE TABLE IF NOT EXISTS inbox (
  item_key TEXT PRIMARY KEY, state TEXT NOT NULL, item TEXT NOT NULL, received_at TEXT, taken_at TEXT, item_id TEXT);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _x(self, sql: str, args: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(args))

    # ---- meta ----
    def get_meta(self, k: str, default: Any = None) -> Any:
        r = self._x("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def set_meta(self, k: str, v: Any) -> None:
        self._x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v, ensure_ascii=False)))

    # ---- runs ----
    def create_run(self, run_id: str, *, state: str = "planned", planned_at: str | None = None, trigger: str = "schedule", reason: str | None = None) -> None:
        self._x("INSERT INTO runs(run_id,state,reason,planned_at,trigger_kind) VALUES(?,?,?,?,?)", (run_id, state, reason, planned_at or timeutil.now_ts(), trigger))

    def set_run_state(self, run_id: str, state: str, reason: str | None = None) -> None:
        now = timeutil.now_ts()
        started = ", started_at=COALESCE(started_at, ?)" if state in ("launching", "running") else ""
        ended = ", ended_at=?" if state in ("completed", "failed", "timed_out", "cancelled", "lost", "skipped", "ejected", "missed") else ""
        args: list[Any] = [state, reason]
        if started:
            args.append(now)
        if ended:
            args.append(now)
        args.append(run_id)
        self._x(f"UPDATE runs SET state=?, reason=?{started}{ended} WHERE run_id=?", args)

    def get_run(self, run_id: str) -> dict | None:
        r = self._x("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def runs(self, states: Iterable[str] | None = None) -> list[dict]:
        if states is None:
            rows = self._x("SELECT * FROM runs ORDER BY planned_at, run_id").fetchall()
        else:
            st = list(states)
            rows = self._x(f"SELECT * FROM runs WHERE state IN ({','.join('?' * len(st))}) ORDER BY planned_at, run_id", st).fetchall() if st else []
        return [dict(r) for r in rows]

    # ---- items ----
    def add_item(self, item_id: str, run_id: str | None, data: dict, *, todo_id: str | None = None, depends_on: list[str] | None = None, state: str = "planned") -> bool:
        now = timeutil.now_ts()
        try:
            self._x("INSERT INTO items(item_id,run_id,state,data,todo_id,depends_on,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (item_id, run_id, state, json.dumps(data, ensure_ascii=False), todo_id, json.dumps(depends_on or []), now, now))
            return True
        except sqlite3.IntegrityError:
            return False

    def _item(self, r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        d["data"] = json.loads(d["data"])
        d["depends_on"] = json.loads(d["depends_on"])
        d["artifacts"] = json.loads(d["artifacts"])
        return d

    def get_item(self, item_id: str) -> dict | None:
        return self._item(self._x("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone())

    def items(self, states: Iterable[str] | None = None, run_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM items", []
        cond = []
        if states is not None:
            st = list(states)
            if not st:
                return []
            cond.append(f"state IN ({','.join('?' * len(st))})")
            args += st
        if run_id:
            cond.append("run_id=?")
            args.append(run_id)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY created_at, item_id"
        return [self._item(r) for r in self._x(q, args).fetchall()]  # type: ignore[misc]

    def set_item_state(self, item_id: str, state: str, reason: str | None = None) -> None:
        now = timeutil.now_ts()
        self._x("UPDATE items SET state=?, reason=?, updated_at=?, ejected_at=CASE WHEN ?='ejected' THEN ? ELSE ejected_at END WHERE item_id=?",
                (state, reason, now, state, now, item_id))

    def update_item(self, item_id: str, *, current_node: str | None = None, started: bool | None = None, hops: int | None = None,
                    artifacts: dict | None = None, data: dict | None = None) -> None:
        sets, args = ["updated_at=?"], [timeutil.now_ts()]
        if current_node is not None:
            sets.append("current_node=?")
            args.append(current_node)
        if started is not None:
            sets.append("started=?")
            args.append(1 if started else 0)
        if hops is not None:
            sets.append("hops=?")
            args.append(hops)
        if artifacts is not None:
            sets.append("artifacts=?")
            args.append(json.dumps(artifacts, ensure_ascii=False))
        if data is not None:
            sets.append("data=?")
            args.append(json.dumps(data, ensure_ascii=False))
        args.append(item_id)
        self._x(f"UPDATE items SET {', '.join(sets)} WHERE item_id=?", args)

    # ---- node_runs ----
    def start_node_run(self, run_id: str, item_id: str, node_id: str, attempt: int, continuation: int = 0) -> int:
        cur = self._x("INSERT INTO node_runs(run_id,item_id,node_id,attempt,continuation,state,started_at) VALUES(?,?,?,?,?,?,?)",
                      (run_id, item_id, node_id, attempt, continuation, "running", timeutil.now_ts()))
        return int(cur.lastrowid)

    def set_launching(self, nr_id: int, key: str, ref: dict | None = None) -> None:
        self._x("UPDATE node_runs SET state='launching', launching_key=?, external_ref=? WHERE id=?", (key, json.dumps(ref or {}, ensure_ascii=False), nr_id))

    def set_ref(self, nr_id: int, ref: dict) -> None:
        """外部の ID（worker-pool の job_id など）を、状態を変えずに残す。落ちた後の引き継ぎ（worker.run）に使う。"""
        self._x("UPDATE node_runs SET external_ref=? WHERE id=?", (json.dumps(ref, ensure_ascii=False), nr_id))

    def finish_node_run(self, nr_id: int, state: str, status: str | None = None, out: dict | None = None, error: dict | None = None) -> None:
        self._x("UPDATE node_runs SET state=?, status=?, out=?, error=?, ended_at=? WHERE id=?",
                (state, status, json.dumps(out, ensure_ascii=False) if out is not None else None,
                 json.dumps(error, ensure_ascii=False) if error is not None else None, timeutil.now_ts(), nr_id))

    def get_node_run(self, nr_id: int) -> dict | None:
        r = self._x("SELECT * FROM node_runs WHERE id=?", (nr_id,)).fetchone()
        return self._nr(r)

    def _nr(self, r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in ("out", "error", "external_ref"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def node_runs(self, item_id: str | None = None, node_id: str | None = None, state: str | None = None) -> list[dict]:
        cond, args = [], []
        for col, val in (("item_id", item_id), ("node_id", node_id), ("state", state)):
            if val is not None:
                cond.append(f"{col}=?")
                args.append(val)
        q = "SELECT * FROM node_runs" + (" WHERE " + " AND ".join(cond) if cond else "") + " ORDER BY id"
        return [self._nr(r) for r in self._x(q, args).fetchall()]  # type: ignore[misc]

    def pending_launching(self) -> list[dict]:
        return self.node_runs(state="launching")

    def unfinished_node_runs(self) -> list[dict]:
        """running のまま残った物（落ちた時に実行中だった）。"""
        return self.node_runs(state="running")

    def node_outputs(self, item_id: str) -> dict[str, dict]:
        """項目の各ノードの最新の成功 (ok/skip) の出力。`{{nodes.ID.data.X}}` の元。"""
        out: dict[str, dict] = {}
        for nr in self.node_runs(item_id=item_id):
            if nr["state"] == "done" and nr["out"] is not None and nr["status"] in ("ok", "skip"):
                out[nr["node_id"]] = nr["out"]
        return out

    def visited(self, item_id: str, node_id: str) -> bool:
        return self._x("SELECT 1 FROM node_runs WHERE item_id=? AND node_id=? LIMIT 1", (item_id, node_id)).fetchone() is not None

    def next_attempt(self, item_id: str, node_id: str) -> int:
        r = self._x("SELECT COALESCE(MAX(attempt),0)+1 AS a FROM node_runs WHERE item_id=? AND node_id=?", (item_id, node_id)).fetchone()
        return int(r["a"])

    # ---- 辺の循環 ----
    def bump_edge(self, item_id: str, edge_key: str) -> int:
        with self._lock:
            self._x("INSERT INTO edge_counts(item_id,edge_key,n) VALUES(?,?,1) ON CONFLICT(item_id,edge_key) DO UPDATE SET n=n+1", (item_id, edge_key))
            return int(self._x("SELECT n FROM edge_counts WHERE item_id=? AND edge_key=?", (item_id, edge_key)).fetchone()["n"])

    def edge_count(self, item_id: str, edge_key: str) -> int:
        r = self._x("SELECT n FROM edge_counts WHERE item_id=? AND edge_key=?", (item_id, edge_key)).fetchone()
        return int(r["n"]) if r else 0

    # ---- 受け取り待ち（inbox。D-6.2）----
    def inbox_put(self, item_key: str, item: dict, *, inbox_max: int) -> str:
        """'accepted' | 'duplicate' | 'full'。同じ item_key は 1 回だけ受け付ける（冪等）。"""
        with self._lock:
            if self._x("SELECT 1 FROM inbox WHERE item_key=?", (item_key,)).fetchone():
                return "duplicate"
            n = self._x("SELECT COUNT(*) AS n FROM inbox WHERE state='queued'").fetchone()["n"]
            if n >= inbox_max:
                return "full"
            self._x("INSERT INTO inbox(item_key,state,item,received_at) VALUES(?,?,?,?)", (item_key, "queued", json.dumps(item, ensure_ascii=False), timeutil.now_ts()))
            return "accepted"

    def inbox_queued(self) -> int:
        return int(self._x("SELECT COUNT(*) AS n FROM inbox WHERE state='queued'").fetchone()["n"])

    def inbox_take(self, n: int) -> list[dict]:
        """queued を古い順に n 件、taken にして返す。"""
        if n <= 0:
            return []
        with self._lock:
            rows = self._x("SELECT item_key, item FROM inbox WHERE state='queued' ORDER BY received_at, item_key LIMIT ?", (n,)).fetchall()
            out = []
            now = timeutil.now_ts()
            for r in rows:
                self._x("UPDATE inbox SET state='taken', taken_at=?, item_id=? WHERE item_key=?", (now, r["item_key"], r["item_key"]))
                out.append(json.loads(r["item"]))
            return out

    def inbox_state(self, item_key: str) -> str | None:
        r = self._x("SELECT state FROM inbox WHERE item_key=?", (item_key,)).fetchone()
        return r["state"] if r else None
