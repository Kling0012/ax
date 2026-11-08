"""時系列記録の保存（SQLite）。追記専用（S-3.1、D-3）。

- events 表: 追記だけ。更新と削除は DB のトリガーでも拒否する（コードにも更新・削除の SQL を持たない。T-1 がソースを検査する）。
  seq は INTEGER PRIMARY KEY AUTOINCREMENT の自動の値（単調に増え、再利用しない）。doc に事象の JSON 全体（seq 以外）を持つ。
- projections 表: S-3.6 の 5 つの表示用データ。追記と同じトランザクションの中で、seq の順に更新する。
- meta 表: projection_skipped の数。
- 書き込みの前に呼び出し側が秘密検査（axcommon.redact）を通した事象を渡す（Store.append の redactor 引数でも通せる）。
- 作り直し: rebuild_projections() は projections を空にして、退避済みの区間（archive の JSON Lines）を先に、次に DB の事象を seq の順に読み直す。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from . import projectors

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  project TEXT,
  subloop TEXT,
  component TEXT NOT NULL,
  doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, seq);
CREATE INDEX IF NOT EXISTS events_subject ON events(subject, seq);
CREATE INDEX IF NOT EXISTS events_subloop ON events(subloop, seq);
CREATE INDEX IF NOT EXISTS events_project ON events(project, seq);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TABLE IF NOT EXISTS projections (
  name TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  upto_seq INTEGER NOT NULL,
  PRIMARY KEY (name, key)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

EVENT_ORDER = ("id", "seq", "ts", "source", "project", "subloop", "kind", "subject", "data", "corr", "cause")


class RebuildError(Exception):
    """作り直しを続けられない（退避済みの書き出しが壊れている、など）。表は元のまま。"""


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _public(doc: dict, seq: int) -> dict:
    out: dict[str, Any] = {}
    full = dict(doc)
    full["seq"] = seq
    for k in EVENT_ORDER:
        if k in full:
            out[k] = full[k]
    for k, v in full.items():
        if k not in out:
            out[k] = v
    return out


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- 追記 -----------------------------------------------------------------------------------
    def append(self, events: Iterable[dict], *, redactor: Callable[[Any], Any] | None = None) -> list[dict]:
        """事象を追記する。戻り値は事象ごとの {id, seq, duplicate}。
        同じ id が既にあれば何もせず duplicate=True（成功）。redactor があれば data に通してから書く。
        投影は同じトランザクションの中で、seq の順に更新する。"""
        evs = list(events)
        results: list[dict] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for ev in evs:
                    doc = {k: v for k, v in ev.items() if k != "seq"}
                    if redactor is not None:
                        doc["data"] = redactor(doc.get("data", {}))
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO events(id, ts, kind, subject, project, subloop, component, doc) VALUES (?,?,?,?,?,?,?,?)",
                        (doc["id"], doc["ts"], doc["kind"], doc["subject"], doc.get("project"), doc.get("subloop"),
                         doc["source"]["component"], _dumps(doc)))
                    if cur.rowcount == 0:
                        row = self._conn.execute("SELECT seq FROM events WHERE id=?", (doc["id"],)).fetchone()
                        results.append({"id": doc["id"], "seq": row["seq"], "duplicate": True})
                        continue
                    seq = cur.lastrowid
                    results.append({"id": doc["id"], "seq": seq, "duplicate": False})
                    self._project_one(doc, seq)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return results

    def _project_one(self, doc: dict, seq: int) -> None:
        p = projectors.PROJECTORS.get(doc["kind"])
        if p is None:
            return
        try:
            key = p.key(doc)
            row = self._conn.execute("SELECT value FROM projections WHERE name=? AND key=?", (p.name, key)).fetchone()
            cur = json.loads(row["value"]) if row else None
            value = p.update(cur, doc)
        except projectors.Skip:
            self._bump_skipped(1)
            return
        self._conn.execute(
            "INSERT INTO projections(name, key, value, upto_seq) VALUES (?,?,?,?) "
            "ON CONFLICT(name, key) DO UPDATE SET value=excluded.value, upto_seq=excluded.upto_seq",
            (p.name, key, _dumps(value), seq))

    def _bump_skipped(self, n: int) -> None:
        self._conn.execute(
            "INSERT INTO meta(k, v) VALUES ('projection_skipped', ?) "
            "ON CONFLICT(k) DO UPDATE SET v = CAST(CAST(v AS INTEGER) + ? AS TEXT)", (str(n), n))

    # --- 読み出し -------------------------------------------------------------------------------
    def max_seq(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]

    def min_seq(self) -> int | None:
        with self._lock:
            return self._conn.execute("SELECT MIN(seq) FROM events").fetchone()[0]

    def projection_skipped(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k='projection_skipped'").fetchone()
        return int(row["v"]) if row else 0

    def get_event(self, event_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT seq, doc FROM events WHERE id=?", (event_id,)).fetchone()
        return _public(json.loads(row["doc"]), row["seq"]) if row else None

    def list_events(self, *, after: int = 0, kinds: list[str] | None = None, subloop: str | None = None, project: str | None = None,
                    subject: str | None = None, limit: int = 200) -> list[dict]:
        where, args = ["seq > ?"], [after]
        if kinds:
            where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            args += kinds
        if subloop is not None:
            where.append("subloop = ?")
            args.append(subloop)
        if project is not None:
            where.append("project = ?")
            args.append(project)
        if subject is not None:
            where.append("subject = ?")
            args.append(subject)
        sql = "SELECT seq, doc FROM events WHERE " + " AND ".join(where) + " ORDER BY seq LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_public(json.loads(r["doc"]), r["seq"]) for r in rows]

    def iter_events(self, after: int = 0, batch: int = 1000) -> Iterator[dict]:
        cur = after
        while True:
            chunk = self.list_events(after=cur, limit=batch)
            if not chunk:
                return
            yield from chunk
            cur = chunk[-1]["seq"]

    def projection(self, name: str, *, key: str | None = None, active: bool = False, limit: int = 200) -> dict:
        """S-3.6 の応答 {name, upto_seq, rows}。upto_seq は応答の時点の最大の seq。
        active=True は value.active が真の行を、limit を無視してすべて返す。"""
        if name not in projectors.PROJECTION_NAMES:
            raise KeyError(name)
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                upto = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
                sql = "SELECT key, value, upto_seq FROM projections WHERE name=?"
                args: list[Any] = [name]
                if key is not None:
                    sql += " AND key=?"
                    args.append(key)
                sql += " ORDER BY upto_seq DESC, key"
                rows = self._conn.execute(sql, args).fetchall()
            finally:
                self._conn.execute("COMMIT")
        out = []
        for r in rows:
            value = json.loads(r["value"])
            if active and not value.get("active"):
                continue
            out.append({"key": r["key"], "value": value, "upto_seq": r["upto_seq"]})
        if not active:
            out = out[: int(limit)]
        return {"name": name, "upto_seq": upto, "rows": out}

    def projection_snapshot(self) -> dict[tuple[str, str], tuple[dict, int]]:
        """全行 {(name, key): (value, upto_seq)}。作り直しの前後の比較に使う。"""
        with self._lock:
            rows = self._conn.execute("SELECT name, key, value, upto_seq FROM projections").fetchall()
        return {(r["name"], r["key"]): (json.loads(r["value"]), r["upto_seq"]) for r in rows}

    # --- 作り直し -------------------------------------------------------------------------------
    def rebuild_projections(self, archived: Iterable[dict] | None = None) -> dict:
        """projections を空にして作り直す（ax-admin rebuild-projections）。

        archived: 退避済みの区間の事象（seq の順。seq を持つ）。先に適用する。続けて DB の事象のうち、
        適用済みの最大の seq より後を seq の順に適用する（DB に退避済みの区間が残っていても二重にならない）。
        失敗したら表は元のまま（トランザクションを戻す）。戻り値は {events, rows, skipped, archived_events}。"""
        rows: dict[tuple[str, str], tuple[dict, int]] = {}
        skipped = applied = arch_n = last = 0
        with self._lock:
            # 読み始める前に書き込みの鍵を取る（別のプロセスの追記が、読んだ後・書く前に割り込まないように）
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for ev in (archived or ()):
                    seq = int(ev["seq"])
                    if seq <= last:
                        continue
                    if projectors.apply_event(rows, ev, seq) is False:
                        skipped += 1
                    last = seq
                    applied += 1
                    arch_n += 1
                for ev in self.iter_events(after=last):
                    if projectors.apply_event(rows, ev, ev["seq"]) is False:
                        skipped += 1
                    last = ev["seq"]
                    applied += 1
                self._conn.execute("DELETE FROM projections")
                self._conn.executemany(
                    "INSERT INTO projections(name, key, value, upto_seq) VALUES (?,?,?,?)",
                    [(n, k, _dumps(v), u) for (n, k), (v, u) in sorted(rows.items())])
                self._conn.execute(
                    "INSERT INTO meta(k, v) VALUES ('projection_skipped', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(skipped),))
                self._conn.execute("COMMIT")
            except (KeyError, ValueError, TypeError) as e:
                self._conn.execute("ROLLBACK")
                raise RebuildError(f"作り直しの入力が壊れている: {type(e).__name__}") from e
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return {"events": applied, "rows": len(rows), "skipped": skipped, "archived_events": arch_n}
