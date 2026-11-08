"""Durable collection/checkpoint journal; all scheduling decisions use BEGIN IMMEDIATE."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from axcommon.timeutil import format_ts, parse_ts
from datetime import datetime, timezone

from .models import EXECUTION_STATES, MODES, ItemKey, Snapshot, TargetRegistry, epoch, paths_overlap


def timestamp(value):
    return format_ts(datetime.fromtimestamp(value, timezone.utc))


class CollectionStore:
    def __init__(self, path: str | Path, *, clock=epoch):
        self.path = str(path)
        self.clock = clock
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS items (
                    key TEXT PRIMARY KEY, implementation_repo TEXT NOT NULL,
                    received_at REAL NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
                    version TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'planned', hold TEXT, reason TEXT,
                    retry_at REAL NOT NULL DEFAULT 0,
                    owner TEXT, lease_token TEXT, lease_until REAL,
                    paths TEXT NOT NULL DEFAULT '["**"]', checkpoint TEXT
                );
                CREATE TABLE IF NOT EXISTS cycles (
                    id TEXT PRIMARY KEY, started_at REAL NOT NULL,
                    starts INTEGER NOT NULL DEFAULT 0, model_calls INTEGER NOT NULL DEFAULT 0,
                    start_limit INTEGER NOT NULL, model_limit INTEGER NOT NULL,
                    seconds REAL NOT NULL, mode TEXT NOT NULL, dry_run INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    key TEXT NOT NULL, generation INTEGER NOT NULL, name TEXT NOT NULL,
                    state TEXT NOT NULL, external_id TEXT, request TEXT NOT NULL,
                    PRIMARY KEY(key,generation,name)
                );
                CREATE TABLE IF NOT EXISTS comments (
                    key TEXT NOT NULL, id TEXT NOT NULL, version TEXT NOT NULL,
                    PRIMARY KEY(key,id)
                );
            """)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, key: ItemKey) -> dict | None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM items WHERE key=?", (key.encode(),)).fetchone()
            return dict(row) if row else None

    def rows(self) -> list[dict]:
        with self.transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM items ORDER BY received_at,key")]

    def collect(self, snapshot: Snapshot, registry: TargetRegistry) -> bool:
        target = registry.resolve(snapshot.key)
        if target is None:
            return False
        data = json.dumps({"labels": list(snapshot.labels), "external_state": snapshot.external_state,
                           "body": snapshot.body, "implementation_pr": snapshot.implementation_pr}, sort_keys=True)
        with self.transaction() as db:
            existing = db.execute("SELECT implementation_repo FROM items WHERE key=?", (snapshot.key.encode(),)).fetchone()
            if existing and existing[0] != target.implementation_repo:
                db.execute("UPDATE items SET hold='parked',reason='mapping_changed' WHERE key=?", (snapshot.key.encode(),))
                return False
            db.execute("""INSERT INTO items(key,implementation_repo,received_at,version,payload)
                VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET version=excluded.version,payload=excluded.payload""",
                (snapshot.key.encode(), target.implementation_repo, self.clock(), snapshot.version, data))
            return True

    def begin_cycle(self, cycle_id: str, *, change_starts_per_cycle=4,
                    change_model_calls_per_cycle=30, change_cycle_seconds=300,
                    mode="run", dry_run=False):
        if mode not in MODES:
            raise ValueError("invalid mode")
        if (type(change_starts_per_cycle) is not int or change_starts_per_cycle < 0 or
            type(change_model_calls_per_cycle) is not int or change_model_calls_per_cycle < 0 or
            type(change_cycle_seconds) not in {int, float} or not math.isfinite(change_cycle_seconds) or change_cycle_seconds < 0):
            raise ValueError("invalid cycle budget")
        with self.transaction() as db:
            old = db.execute("SELECT * FROM cycles WHERE id=?", (cycle_id,)).fetchone()
            settings = (change_starts_per_cycle, change_model_calls_per_cycle, change_cycle_seconds, mode, int(dry_run))
            if old:
                if tuple(old[x] for x in ("start_limit", "model_limit", "seconds", "mode", "dry_run")) != settings:
                    raise ValueError("cycle_id reused with different settings")
                return
            db.execute("INSERT INTO cycles(id,started_at,start_limit,model_limit,seconds,mode,dry_run) VALUES(?,?,?,?,?,?,?)",
                       (cycle_id, self.clock(), *settings))

    def cycle(self, cycle_id: str) -> dict:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM cycles WHERE id=?", (cycle_id,)).fetchone()
            if row is None:
                raise KeyError(cycle_id)
            return dict(row)

    def consume_model_calls(self, cycle_id: str, count=1) -> bool:
        if type(count) is not int or count < 1:
            raise ValueError("positive model call count required")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM cycles WHERE id=?", (cycle_id,)).fetchone()
            if row is None:
                raise KeyError(cycle_id)
            if (row["mode"] != "run" or row["dry_run"] or self.clock() >= row["started_at"] + row["seconds"] or
                row["model_calls"] + count > row["model_limit"]):
                return False
            db.execute("UPDATE cycles SET model_calls=model_calls+? WHERE id=?", (count, cycle_id))
            return True

    def waiting(self, registry: TargetRegistry, *, db=None) -> list[dict]:
        if db is None:
            with self.transaction() as conn:
                return self.waiting(registry, db=conn)
        rows = []
        for record in db.execute("SELECT * FROM items WHERE state='planned' AND (hold IS NULL OR hold='retry') AND retry_at<=?", (self.clock(),)):
            row = dict(record)
            key = ItemKey.decode(row["key"])
            target = registry.resolve(key)
            if target is None or target.implementation_repo != row["implementation_repo"]:
                continue
            if json.loads(row["payload"])["external_state"] != "open":
                continue  # Closed comments are collected, but reopening requires the safety path.
            labels = set(json.loads(row["payload"])["labels"])
            priority = next((i for i, label in enumerate(target.priority_labels) if label in labels), len(target.priority_labels))
            rows.append((priority, row["received_at"], key, row))
        return [entry[3] for entry in sorted(rows, key=lambda entry: entry[:3])]

    def claim_next(self, cycle_id: str, registry: TargetRegistry, owner: str, *, lease_seconds=60,
                   paths_by_key: dict[str, list[str]] | None = None) -> dict | None:
        if not owner or lease_seconds <= 0:
            raise ValueError("owner and positive lease required")
        paths_by_key = paths_by_key or {}
        with self.transaction() as db:
            cycle = db.execute("SELECT * FROM cycles WHERE id=?", (cycle_id,)).fetchone()
            if cycle is None:
                raise KeyError(cycle_id)
            if (cycle["mode"] != "run" or cycle["dry_run"] or cycle["starts"] >= cycle["start_limit"] or
                cycle["model_calls"] >= cycle["model_limit"] or self.clock() >= cycle["started_at"] + cycle["seconds"]):
                return None
            active = [dict(row) for row in db.execute("SELECT * FROM items WHERE owner IS NOT NULL")]
            for item in self.waiting(registry, db=db):
                key = ItemKey.decode(item["key"])
                target = registry.resolve(key)
                unresolved = db.execute("SELECT 1 FROM operations WHERE key=? AND state IN ('launching','submitted','running','lost')", (item["key"],)).fetchone()
                if unresolved:
                    continue
                if sum(ItemKey.decode(row["key"]).issue_repo == key.issue_repo for row in active) >= target.execution_cap:
                    continue
                paths = paths_by_key.get(item["key"], [target.implementation_repo + "/" + path for path in target.allowed_paths])
                if not paths:
                    paths = ["**"]
                if any(row["implementation_repo"] == target.implementation_repo or paths_overlap(paths, json.loads(row["paths"])) for row in active):
                    db.execute("UPDATE items SET retry_at=?,reason='conflict' WHERE key=?", (self.clock() + lease_seconds, item["key"]))
                    continue
                token = uuid4().hex
                expires = self.clock() + lease_seconds
                db.execute("UPDATE items SET owner=?,lease_token=?,lease_until=?,paths=?,state='launching',hold=NULL,reason=NULL WHERE key=?",
                           (owner, token, expires, json.dumps(paths), item["key"]))
                db.execute("INSERT INTO operations(key,generation,name,state,request) VALUES(?,?,'work','launching','{}')",
                           (item["key"], item["generation"]))
                db.execute("UPDATE cycles SET starts=starts+1 WHERE id=?", (cycle_id,))
                return {**item, "owner": owner, "lease_token": token, "lease_until": expires, "paths": json.dumps(paths), "state": "launching"}
        return None

    def _owned(self, db, claim):
        row = db.execute("SELECT * FROM items WHERE key=? AND owner=? AND lease_token=? AND generation=?",
                         (claim["key"], claim["owner"], claim["lease_token"], claim["generation"])).fetchone()
        if row is None or row["lease_until"] <= self.clock() or row["state"] == "lost":
            raise ValueError("stale_lease")
        return row

    def validate_lease(self, claim) -> bool:
        with self.transaction() as db:
            self._owned(db, claim)
        return True

    def renew(self, claim, *, lease_seconds=60):
        if lease_seconds <= 0:
            raise ValueError("positive lease required")
        with self.transaction() as db:
            self._owned(db, claim)
            db.execute("UPDATE items SET lease_until=? WHERE key=?", (self.clock() + lease_seconds, claim["key"]))

    def record_submission(self, claim, external_id: str):
        if not external_id:
            raise ValueError("external_id required")
        with self.transaction() as db:
            self._owned(db, claim)
            op = db.execute("SELECT * FROM operations WHERE key=? AND generation=? AND name='work'", (claim["key"], claim["generation"])).fetchone()
            if op["external_id"] and op["external_id"] != external_id:
                raise ValueError("external_id conflict")
            db.execute("UPDATE operations SET state='submitted',external_id=? WHERE key=? AND generation=? AND name='work'",
                       (external_id, claim["key"], claim["generation"]))
            db.execute("UPDATE items SET state='submitted' WHERE key=?", (claim["key"],))

    def finish(self, claim, state="completed", *, reason=None):
        if state not in {"completed", "failed", "timed_out", "cancelled", "lost", "ejected"}:
            raise ValueError("terminal state required")
        with self.transaction() as db:
            self._owned(db, claim)
            if state == "lost":
                db.execute("UPDATE items SET state='lost',reason=?,hold='parked' WHERE key=?", (reason, claim["key"]))
            else:
                db.execute("UPDATE items SET state=?,reason=?,hold=NULL,owner=NULL,lease_token=NULL,lease_until=NULL WHERE key=?",
                           (state, reason, claim["key"]))
            db.execute("UPDATE operations SET state=? WHERE key=? AND generation=? AND name='work'", (state, claim["key"], claim["generation"]))

    def hold(self, key, status, *, reason, check_at):
        if status not in {"parked", "waiting_user", "waiting_external", "retry"} or not reason:
            raise ValueError("hold status and reason required")
        when = parse_ts(check_at).timestamp()
        with self.transaction() as db:
            db.execute("UPDATE items SET hold=?,reason=?,retry_at=? WHERE key=?", (status, reason, when, key.encode()))

    def reopen(self, key, *, authorized: bool):
        """Call only after the downstream safety/requirements decision, never on collection."""
        if not authorized:
            raise PermissionError("reopen decision required")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM items WHERE key=?", (key.encode(),)).fetchone()
            if row is None or row["owner"] or row["state"] in {"launching", "submitted", "running", "lost"}:
                raise ValueError("item is not resumable")
            db.execute("UPDATE items SET state='planned',hold=NULL,reason=NULL,retry_at=0,generation=generation+1 WHERE key=?", (key.encode(),))

    def save_checkpoint(self, claim, *, verified_results: list[str], data: dict):
        if not verified_results or not all(isinstance(x, str) and x for x in verified_results):
            raise ValueError("verified result references required")
        with self.transaction() as db:
            row = self._owned(db, claim)
            if row["version"] != claim["version"]:
                raise ValueError("checkpoint_version_changed")
            value = {"version": row["version"], "implementation_repo": row["implementation_repo"],
                     "verified_results": verified_results, "data": data}
            db.execute("UPDATE items SET checkpoint=? WHERE key=?", (json.dumps(value, sort_keys=True), claim["key"]))

    def resume_checkpoint(self, key, *, version: str, verified_results: list[str]) -> dict | None:
        row = self.get(key)
        if not row or not row["checkpoint"]:
            return None
        value = json.loads(row["checkpoint"])
        if (value["version"] != version or row["version"] != version or value["implementation_repo"] != row["implementation_repo"] or
            value["verified_results"] != verified_results):
            return None
        return value["data"]

    def operation(self, key, name, generation=None):
        with self.transaction() as db:
            if generation is None:
                generation = db.execute("SELECT generation FROM items WHERE key=?", (key.encode(),)).fetchone()[0]
            row = db.execute("SELECT * FROM operations WHERE key=? AND generation=? AND name=?", (key.encode(), generation, name)).fetchone()
            return dict(row) if row else None

    def prepare_operation(self, key, name, request: dict, *, replace_completed=False) -> dict | None:
        """One writer wins. An existing launching/lost intent is NEVER sent again."""
        with self.transaction() as db:
            item = db.execute("SELECT * FROM items WHERE key=?", (key.encode(),)).fetchone()
            if item is None:
                raise KeyError(key)
            old = db.execute("SELECT * FROM operations WHERE key=? AND generation=? AND name=?", (key.encode(), item["generation"], name)).fetchone()
            if old:
                if not replace_completed or old["state"] != "completed":
                    return None
                db.execute("UPDATE operations SET state='launching',external_id=NULL,request=? WHERE key=? AND generation=? AND name=?",
                           (json.dumps(request, sort_keys=True), key.encode(), item["generation"], name))
            else:
                db.execute("INSERT INTO operations(key,generation,name,state,request) VALUES(?,?,?,'launching',?)",
                           (key.encode(), item["generation"], name, json.dumps(request, sort_keys=True)))
            return {"key": key.encode(), "generation": item["generation"], "name": name, "request": json.dumps(request), "state": "launching", "external_id": None}

    def update_operation(self, operation, state, external_id=None):
        if state not in EXECUTION_STATES:
            raise ValueError("invalid state")
        with self.transaction() as db:
            current = db.execute("SELECT state FROM operations WHERE key=? AND generation=? AND name=?",
                                 (operation["key"], operation["generation"], operation["name"])).fetchone()
            if current is None or current[0] not in {"launching", "submitted", "running"}:
                return
            db.execute("UPDATE operations SET state=?,external_id=COALESCE(?,external_id) WHERE key=? AND generation=? AND name=?",
                       (state, external_id, operation["key"], operation["generation"], operation["name"]))
            if state == "lost":
                db.execute("UPDATE items SET hold='parked',reason='external_operation_lost' WHERE key=?", (operation["key"],))

    def pending_operations(self):
        with self.transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM operations WHERE state IN ('launching','submitted','running') OR (name='work' AND state='lost' AND external_id IS NOT NULL)")]

    def reconcile_work(self, operation, observation, *, lease_seconds=60):
        """Recovery preserves ownership for confirmed live work, even after lease expiry."""
        state = observation.state
        with self.transaction() as db:
            current = db.execute("SELECT state,external_id FROM operations WHERE key=? AND generation=? AND name='work'",
                                 (operation["key"], operation["generation"])).fetchone()
            if current is None or current[0] not in {"launching", "submitted", "running", "lost"} or current[1] != operation["external_id"]:
                return
            db.execute("UPDATE operations SET state=?,external_id=COALESCE(?,external_id) WHERE key=? AND generation=? AND name='work'",
                       (state, observation.external_id, operation["key"], operation["generation"]))
            if state in {"submitted", "running"}:
                db.execute("UPDATE items SET state=?,hold=NULL,reason=NULL,lease_until=? WHERE key=? AND generation=?",
                           (state, self.clock() + lease_seconds, operation["key"], operation["generation"]))
            elif state == "lost":
                # Unknown work may still be writing. Keep its repository/path reservation.
                db.execute("UPDATE items SET state='lost',hold='parked',reason='external_operation_lost' WHERE key=? AND generation=?",
                           (operation["key"], operation["generation"]))
            else:
                db.execute("UPDATE items SET state=?,hold=NULL,reason=NULL,owner=NULL,lease_token=NULL,lease_until=NULL WHERE key=? AND generation=?",
                           (state, operation["key"], operation["generation"]))

    def new_comments(self, key, comments) -> list[dict]:
        """Delivery is explicit-ack: a crash before ack causes a safe redelivery."""
        with self.transaction() as db:
            seen = {row[0]: row[1] for row in db.execute("SELECT id,version FROM comments WHERE key=?", (key.encode(),))}
        return [comment for comment in comments if not comment.get("is_bot", False) and
                seen.get(str(comment["id"])) != json.dumps(comment, sort_keys=True)]

    def acknowledge_comments(self, key, comments):
        with self.transaction() as db:
            for comment in comments:
                db.execute("INSERT INTO comments(key,id,version) VALUES(?,?,?) ON CONFLICT(key,id) DO UPDATE SET version=excluded.version",
                           (key.encode(), str(comment["id"]), json.dumps(comment, sort_keys=True)))
