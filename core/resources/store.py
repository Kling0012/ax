"""資源台帳の保存（SQLite）。資源の状態（status、until、level、stats）を持つ（D-7.3、D-8）。

- resources 表: config/models.yaml を初回だけ読み込んだ後はここが正（D-8）。状態の変化は
  この表の 1 行に書く（別表を持たない）。
- exclusions 表: 除外は理由と期限付きで保持する（S-8.2）。複数あればすべて解除されるまで
  候補に戻さない。期限到達や明示解除は対象の一時除外だけに効く。
- 段階と成績は stats の JSON に持つ（S-8.2 の実績。対象期間、試行 ID、検証結果と出所）。

線形の関係を保つため、更新と読み取りは 1 つの接続でロックする。
秘密の値は持たない。 pricing と説明は保持するが、応答の範囲は S-8.8 のとおり。
"""
from __future__ import annotations

import json
import math
import time
import fcntl
import os
from contextlib import ExitStack
import re
from urllib.parse import urlsplit
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from axcommon import timeutil

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  family TEXT NOT NULL DEFAULT '',
  tier TEXT NOT NULL DEFAULT 'standard',
  level TEXT NOT NULL DEFAULT 'L1',
  billing TEXT NOT NULL DEFAULT 'flat',
  vision INTEGER NOT NULL DEFAULT 0,
  runner TEXT NOT NULL DEFAULT '',
  embedding_endpoint TEXT NOT NULL DEFAULT '',
  cap INTEGER,
  prefix_cache TEXT NOT NULL DEFAULT 'unknown',
  status TEXT NOT NULL DEFAULT 'active',
  trains_on_input TEXT NOT NULL DEFAULT 'unknown',
  description TEXT NOT NULL DEFAULT '',
  pricing TEXT NOT NULL DEFAULT '{}',
  stats TEXT NOT NULL DEFAULT '{}',
  secrets_allowed INTEGER NOT NULL DEFAULT 0,
  data_classes TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcome_receipts (
  resource_id TEXT NOT NULL,
  trial_id TEXT NOT NULL,
  PRIMARY KEY(resource_id, trial_id)
);
CREATE TABLE IF NOT EXISTS effective_slot_limits (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  revision INTEGER NOT NULL,
  expires_at REAL NOT NULL,
  models TEXT NOT NULL,
  families TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS family_caps (
  family TEXT PRIMARY KEY,
  cap INTEGER CHECK(cap IS NULL OR cap >= 0)
);
CREATE TABLE IF NOT EXISTS slot_reservations (
  reservation_id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  family TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('held', 'released'))
);
CREATE TABLE IF NOT EXISTS review_slot_outcomes (
  reservation_id TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK(state IN ('unknown', 'released_after_grace')),
  unknown_at REAL NOT NULL,
  deadline REAL NOT NULL,
  released_at REAL
);
CREATE INDEX IF NOT EXISTS slots_model ON slot_reservations(model, state);
CREATE INDEX IF NOT EXISTS slots_family ON slot_reservations(family, state);
CREATE TABLE IF NOT EXISTS environment_leases (
  lease_id TEXT PRIMARY KEY,
  env_id TEXT NOT NULL,
  holder TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('env-job', 'sandbox')),
  state TEXT NOT NULL CHECK(state IN ('held', 'unknown', 'released')),
  outcome TEXT
);
CREATE INDEX IF NOT EXISTS environment_leases_env ON environment_leases(env_id, state);
CREATE TABLE IF NOT EXISTS exclusions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  resource_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  until TEXT,
  created_at TEXT NOT NULL,
  released_at TEXT,
  keep_status INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS exclusions_resource ON exclusions(resource_id, released_at);
CREATE TABLE IF NOT EXISTS command_exclusions (
  command_id TEXT PRIMARY KEY,
  resource_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  until TEXT NOT NULL,
  released INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS command_exclusions_resource ON command_exclusions(resource_id, released, until);
CREATE TABLE IF NOT EXISTS selections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS selections_job ON selections(job_id);
CREATE TABLE IF NOT EXISTS discovery (
  fetched_at TEXT,
  ok INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  provided TEXT NOT NULL DEFAULT '[]',
  new_ids TEXT NOT NULL DEFAULT '[]',
  matched TEXT NOT NULL DEFAULT '[]',
  unseen TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS weekly_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id TEXT NOT NULL,
  ran_at TEXT NOT NULL,
  reachable INTEGER,
  responded INTEGER,
  tool_ok INTEGER,
  diff TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS weekly_checks_model ON weekly_checks(model_id, ran_at);
"""


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _loads(text: str, default: Any = None) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def _normalized_endpoint(value: str) -> str | None:
    if not isinstance(value, str) or not value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        parsed.port  # Reject malformed ports and authorities.
        match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]+)(.*)$", value)
        if not match or parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        return (match.group(1).lower() + "://" + match.group(2).lower() + match.group(3)).rstrip("/")
    except ValueError:
        return None


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.worker_pool_db = None
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        with self._lock:
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(resources)")}
            if "supported_runners" not in columns:
                self._conn.execute("ALTER TABLE resources ADD COLUMN supported_runners TEXT NOT NULL DEFAULT '[]'")
            if "embedding_endpoint" not in columns:
                self._conn.execute("ALTER TABLE resources ADD COLUMN embedding_endpoint TEXT NOT NULL DEFAULT ''")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _check_cap(cap):
        if cap is not None and (type(cap) is not int or cap < 0):
            raise ValueError("invalid_cap")

    def set_cap(self, kind: str, key: str, cap: int | None) -> None:
        self._check_cap(cap)
        if not isinstance(key, str) or not key or len(key) > 120:
            raise ValueError("invalid_key")
        with self._lock:
            if kind == "model":
                cursor = self._conn.execute("UPDATE resources SET cap=? WHERE id=? AND kind='model'", (cap, key))
                if not cursor.rowcount:
                    raise KeyError(key)
            elif kind == "environment":
                now = timeutil.now_ts()
                self._conn.execute("INSERT OR IGNORE INTO resources(id, kind, created_at, updated_at) VALUES (?, 'environment', ?, ?)", (key, now, now))
                cursor = self._conn.execute("UPDATE resources SET cap=? WHERE id=? AND kind='environment'", (cap, key))
                if not cursor.rowcount:
                    raise KeyError(key)
            elif kind == "family":
                self._conn.execute("INSERT INTO family_caps VALUES (?, ?) ON CONFLICT(family) DO UPDATE SET cap=excluded.cap", (key, cap))
            else:
                raise ValueError("invalid_kind")

    def effective_limits_revision(self):
        with self._lock:
            row = self._conn.execute("SELECT revision FROM effective_slot_limits WHERE singleton=1").fetchone()
            return row[0] if row else 0

    def set_effective_limits(self, revision, expires_at, models, families):
        if type(revision) is not int or revision < 0:
            raise ValueError("invalid_revision")
        if type(expires_at) not in (int, float) or not math.isfinite(expires_at):
            raise ValueError("invalid_expiry")
        for limits in (models, families):
            if not isinstance(limits, dict):
                raise ValueError("invalid_limits")
            for key, cap in limits.items():
                if not isinstance(key, str) or not key or len(key) > 120 or type(cap) is not int or cap < 0:
                    raise ValueError("invalid_limits")
        encoded_models, encoded_families = _dumps(models), _dumps(families)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                old = self._conn.execute("SELECT * FROM effective_slot_limits WHERE singleton=1").fetchone()
                if old and (revision < old['revision'] or (revision == old['revision'] and
                    (expires_at != old['expires_at'] or models != _loads(old['models']) or families != _loads(old['families'])))):
                    raise ValueError("revision_conflict")
                self._conn.execute("INSERT OR REPLACE INTO effective_slot_limits VALUES (1, ?, ?, ?, ?)",
                                   (revision, expires_at, encoded_models, encoded_families))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def acquire(self, env_id: str, holder: str, kind: str) -> str | None:
        if any(not isinstance(x, str) or not x or len(x) > 120 for x in (env_id, holder)) or kind not in ("env-job", "sandbox"):
            raise ValueError("invalid_lease")
        lease_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = timeutil.now_ts()
                self._conn.execute("INSERT OR IGNORE INTO resources(id, kind, created_at, updated_at) VALUES (?, 'environment', ?, ?)", (env_id, now, now))
                exists = self._conn.execute("SELECT 1 FROM resources WHERE id=? AND kind='environment'", (env_id,)).fetchone()
                if not exists:
                    raise KeyError(env_id)
                claimed = self._conn.execute("""UPDATE resources SET cap=cap WHERE id=? AND kind='environment'
                    AND (SELECT COUNT(*) FROM environment_leases WHERE env_id=? AND state IN ('held','unknown')) < COALESCE(cap,1)""", (env_id, env_id)).rowcount
                if claimed:
                    self._conn.execute("INSERT INTO environment_leases VALUES (?, ?, ?, ?, 'held', NULL)", (lease_id, env_id, holder, kind))
                self._conn.execute("COMMIT")
                return lease_id if claimed else None
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def release(self, lease_id: str, outcome: str) -> bool:
        if not isinstance(lease_id, str) or not lease_id or outcome not in ("completed", "failed", "cancelled"):
            raise ValueError("invalid_release")
        with self._lock:
            return bool(self._conn.execute("UPDATE environment_leases SET state='released', outcome=? WHERE lease_id=? AND state IN ('held','unknown')", (outcome, lease_id)).rowcount)

    def hold_unknown(self, lease_id: str) -> bool:
        if not isinstance(lease_id, str) or not lease_id:
            raise ValueError("invalid_lease")
        with self._lock:
            return bool(self._conn.execute("UPDATE environment_leases SET state='unknown' WHERE lease_id=? AND state IN ('held','unknown')", (lease_id,)).rowcount)

    def slots(self) -> dict:
        with self._lock:
            models = self._conn.execute("SELECT id, family, COALESCE(cap, 1) AS cap FROM resources WHERE kind='model'").fetchall()
            limits = self._conn.execute("SELECT * FROM effective_slot_limits WHERE singleton=1").fetchone()
            valid = limits is not None and limits['expires_at'] > time.time()
            model_caps = _loads(limits['models']) if valid else {}
            family_caps = _loads(limits['families']) if valid else {}
            usage = self._conn.execute("SELECT model, family, COUNT(*) AS count FROM slot_reservations WHERE state='held' GROUP BY model, family").fetchall()
            by_model = {r['model']: r['count'] for r in usage}
            by_family = {}
            for r in usage:
                by_family[r['family']] = by_family.get(r['family'], 0) + r['count']
            return {"models": {r['id']: {"family": r['family'], "cap": model_caps.get(r['id'], 0) if r['family'] in family_caps else 0, "used": by_model.get(r['id'], 0)} for r in models},
                    "families": {family: {"cap": family_caps.get(family, 0), "used": by_family.get(family, 0)}
                                 for family in set(family_caps) | {r['family'] for r in models} | set(by_family)}}

    def reserve_slot(self, reservation_id: str, model: str) -> bool:
        return self.reserve_slot_with_reason(reservation_id, model)[0]

    def reserve_slot_with_reason(self, reservation_id: str, model: str) -> tuple[bool, str | None]:
        if not isinstance(reservation_id, str) or not reservation_id or len(reservation_id) > 120:
            raise ValueError("invalid_reservation_id")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                previous = self._conn.execute("SELECT model, state FROM slot_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
                now = timeutil.now_ts()
                excluded = self._conn.execute(
                    "SELECT 1 FROM command_exclusions c WHERE c.released=0 AND c.until>? AND "
                    "(c.resource_id=? OR (c.resource_id=(SELECT family FROM resources WHERE id=?) "
                    "AND NOT EXISTS (SELECT 1 FROM resources WHERE id=c.resource_id))) "
                    "UNION SELECT 1 FROM exclusions WHERE resource_id=? AND released_at IS NULL AND (until IS NULL OR until>?) LIMIT 1",
                    (now, model, model, model, now)).fetchone() is not None
                if previous:
                    if previous['model'] != model:
                        raise ValueError("reservation_conflict")
                    result = previous['state'] == 'held' and not excluded
                    reason = ('model_excluded' if excluded else 'reservation_released') if not result else None
                else:
                    row = self._conn.execute("SELECT family, COALESCE(cap,1) AS cap, status FROM resources WHERE id=? AND kind='model'", (model,)).fetchone()
                    if row is None:
                        raise KeyError(model)
                    family = row['family']
                    model_used = self._conn.execute("SELECT COUNT(*) FROM slot_reservations WHERE model=? AND state='held'", (model,)).fetchone()[0]
                    family_used = self._conn.execute("SELECT COUNT(*) FROM slot_reservations WHERE family=? AND state='held'", (family,)).fetchone()[0]
                    limits = self._conn.execute("SELECT * FROM effective_slot_limits WHERE singleton=1").fetchone()
                    reason = None
                    if limits is None:
                        reason = 'limits_missing'
                    elif limits['expires_at'] <= time.time():
                        reason = 'limits_expired'
                    else:
                        models, families = _loads(limits['models']), _loads(limits['families'])
                        if model not in models or family not in families:
                            reason = 'limits_missing'
                        elif row['status'] not in ('active', 'degraded'):
                            reason = 'model_unavailable'
                        elif excluded:
                            reason = 'model_excluded'
                        elif model_used >= models[model] or family_used >= families[family]:
                            reason = 'capacity_exhausted'
                    result = reason is None
                    if result:
                        self._conn.execute("INSERT INTO slot_reservations VALUES (?, ?, ?, 'held')", (reservation_id, model, family))
                self._conn.execute("COMMIT")
                return result, reason
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def reassign_slot(self, reservation_id: str, previous_model: str, model: str) -> bool:
        if not isinstance(reservation_id, str) or not reservation_id or len(reservation_id) > 120:
            raise ValueError("invalid_reservation_id")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                previous = self._conn.execute("SELECT model, state FROM slot_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
                if previous and previous['model'] not in (previous_model, model):
                    raise ValueError("reservation_conflict")
                row = self._conn.execute("SELECT family, COALESCE(cap,1) AS cap, status FROM resources WHERE id=? AND kind='model'", (model,)).fetchone()
                if row is None:
                    raise KeyError(model)
                now = timeutil.now_ts()
                excluded = self._conn.execute(
                    "SELECT 1 FROM command_exclusions c WHERE c.released=0 AND c.until>? AND "
                    "(c.resource_id=? OR (c.resource_id=? AND NOT EXISTS (SELECT 1 FROM resources WHERE id=c.resource_id))) "
                    "UNION SELECT 1 FROM exclusions WHERE resource_id=? AND released_at IS NULL AND (until IS NULL OR until>?) LIMIT 1",
                    (now, model, row['family'], model, now)).fetchone() is not None
                model_used = self._conn.execute("SELECT COUNT(*) FROM slot_reservations WHERE model=? AND state='held' AND reservation_id<>?", (model, reservation_id)).fetchone()[0]
                family_used = self._conn.execute("SELECT COUNT(*) FROM slot_reservations WHERE family=? AND state='held' AND reservation_id<>?", (row['family'], reservation_id)).fetchone()[0]
                limits = self._conn.execute("SELECT * FROM effective_slot_limits WHERE singleton=1").fetchone()
                models = _loads(limits['models']) if limits and limits['expires_at'] > time.time() else {}
                families = _loads(limits['families']) if limits and limits['expires_at'] > time.time() else {}
                ok = (not excluded and row['status'] in ('active', 'degraded') and model in models
                      and row['family'] in families and model_used < models[model] and family_used < families[row['family']])
                if ok:
                    self._conn.execute("INSERT INTO slot_reservations VALUES (?, ?, ?, 'held') ON CONFLICT(reservation_id) DO UPDATE SET model=excluded.model, family=excluded.family, state='held'", (reservation_id, model, row['family']))
                self._conn.execute("COMMIT")
                return ok
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def release_slot(self, reservation_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE slot_reservations SET state='released' WHERE reservation_id=? AND state='held' AND NOT EXISTS (SELECT 1 FROM review_slot_outcomes WHERE reservation_id=? AND state='unknown')", (reservation_id, reservation_id))
            return bool(cur.rowcount)

    def mark_review_unknown(self, reservation_id: str, *, now: float, grace: float) -> bool:
        if not isinstance(reservation_id, str) or not reservation_id.startswith('publish-review-'):
            raise ValueError('invalid_review_reservation')
        if not isinstance(grace, (int, float)) or not 0 < grace <= 86400:
            raise ValueError('invalid_review_grace')
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                held = self._conn.execute("SELECT 1 FROM slot_reservations WHERE reservation_id=? AND state='held'", (reservation_id,)).fetchone()
                if held:
                    self._conn.execute("INSERT OR IGNORE INTO review_slot_outcomes(reservation_id,state,unknown_at,deadline) VALUES (?,'unknown',?,?)", (reservation_id, now, now + grace))
                self._conn.execute('COMMIT')
                return bool(held)
            except BaseException:
                self._conn.execute('ROLLBACK')
                raise

    def review_slot_outcome(self, reservation_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT reservation_id,state,unknown_at,deadline,released_at FROM review_slot_outcomes WHERE reservation_id=?", (reservation_id,)).fetchone()
            return dict(row) if row else None

    def reconcile_review_slots(self, *, now: float) -> int:
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                rows = self._conn.execute("SELECT reservation_id FROM review_slot_outcomes WHERE state='unknown' AND deadline<=?", (now,)).fetchall()
                for row in rows:
                    ident = row['reservation_id']
                    self._conn.execute("UPDATE slot_reservations SET state='released' WHERE reservation_id=? AND state='held'", (ident,))
                    self._conn.execute("UPDATE review_slot_outcomes SET state='released_after_grace',released_at=? WHERE reservation_id=? AND state='unknown'", (now, ident))
                self._conn.execute('COMMIT')
                return len(rows)
            except BaseException:
                self._conn.execute('ROLLBACK')
                raise

    # --- 登録簿の移行 ---------------------------------------------------------------------------------
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["supported_runners"] = _loads(d.get("supported_runners") or "[]") or ([d["runner"]] if d.get("runner") else [])
        d["vision"] = bool(d.get("vision"))
        d["secrets_allowed"] = bool(d.get("secrets_allowed"))
        d["stats"] = _loads(d.get("stats") or "{}") or {}
        d["pricing"] = _loads(d.get("pricing") or "{}") or {}
        # data_classes は kind=environment のときにだけ保持する（kind=model はモデルの親に持たない）
        if d.get("kind") != "environment":
            d.pop("data_classes", None)
        else:
            d["data_classes"] = _loads(d.get("data_classes") or "[]") or []
        if d.get("cap") is None:
            d.pop("cap", None)
        return d

    def seed(self, models: list[dict], *, now: str | None = None) -> int:
        """初回だけ登録簿を移す。既に資源があるときは買わない（D-8）。移した件数を返す。"""
        now = now or timeutil.now_ts()
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
            if count:
                return 0
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for m in models:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO resources(id, kind, family, tier, level, billing, vision, runner, supported_runners, embedding_endpoint, cap, prefix_cache, status,"
                        " trains_on_input, description, pricing, stats, secrets_allowed, data_classes, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (m["id"], m.get("kind", "model"), m.get("family", ""), m.get("tier", "standard"), m.get("level", "L1"),
                         m.get("billing", "flat"), 1 if m.get("vision") else 0, m.get("runner", ""), _dumps(m.get("supported_runners") or []), m.get("embedding_endpoint", ""), m.get("cap"), m.get("prefix_cache", "unknown"),
                         m.get("status", "active"), m.get("trains_on_input", "unknown"), m.get("description", ""), _dumps(m.get("pricing") or {}),
                         _dumps(m.get("stats") or {}), 1 if m.get("secrets_allowed") else 0, _dumps(m.get("data_classes") or []), now, now),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            return len(models)

    def set_supported_runners(self, model: str, runners: list[str]) -> dict:
        if (not isinstance(model, str) or not isinstance(runners, list) or not runners
                or len(runners) > 20 or len(set(runners)) != len(runners)
                or any(not isinstance(r, str) or not re.fullmatch(r"[a-z0-9-]{1,40}", r) for r in runners)):
            raise ValueError("invalid_runners")
        with self._lock:
            row = self._conn.execute("SELECT runner FROM resources WHERE id=? AND kind='model'", (model,)).fetchone()
            if not row:
                raise KeyError(model)
            if row["runner"] and row["runner"] not in runners:
                raise ValueError("default_runner_required")
            self._conn.execute("UPDATE resources SET supported_runners=?, updated_at=? WHERE id=?", (_dumps(runners), timeutil.now_ts(), model))
        return self.get(model)

    def registered_model(self, provider: str, model: str) -> bool:
        if not isinstance(provider, str) or not provider or "/" in provider or not isinstance(model, str) or not model:
            return False
        try:
            row = self.get(f"{provider}/{model}")
            return bool(row and row["kind"] == "model")
        except Exception:
            return False

    def registered_embedding_endpoint(self, provider: str, model: str, endpoint: str) -> bool:
        if not isinstance(provider, str) or not provider or "/" in provider or not isinstance(model, str) or not model:
            return False
        try:
            requested = _normalized_endpoint(endpoint)
            row = self.get(f"{provider}/{model}")
            registered = _normalized_endpoint(row.get("embedding_endpoint")) if row and row["kind"] == "model" else None
            return bool(requested and registered and requested == registered)
        except Exception:
            return False

    def get(self, resource_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list(self, *, kind: str | None = None, status: str | None = None, family: str | None = None,
             tier: str | None = None, limit: int = 500) -> list[dict]:
        where, args = [], []
        if kind:
            where.append("kind=?")
            args.append(kind)
        if status:
            where.append("status=?")
            args.append(status)
        if family:
            where.append("family=?")
            args.append(family)
        if tier:
            where.append("tier=?")
            args.append(tier)
        sql = "SELECT * FROM resources" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def sync_command_exclusion(self, command_id: str, resource_id: str, *, active: bool,
                               reason: str = "", until: str = "") -> bool:
        # Take the same per-job file locks as worker-pool. Recheck the held
        # reservations in a write transaction because another job can reserve
        # while the locks are being acquired.
        while True:
            with self._lock:
                ids = self._exclusion_reservations(resource_id)
            with ExitStack() as stack:
                if ids and not self.worker_pool_db:
                    raise RuntimeError("worker pool launch locks unavailable")
                for job_id in sorted(ids):
                    if not re.fullmatch(r"[a-z0-9-]{1,40}", job_id):
                        raise RuntimeError("invalid reservation lock id")
                    fd = os.open(self.worker_pool_db + ".launch-" + job_id + ".lock", os.O_CREAT | os.O_RDONLY, 0o644)
                    stack.callback(os.close, fd)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        if not self._exclusion_reservations(resource_id).issubset(ids):
                            self._conn.execute("ROLLBACK")
                            continue
                        result = self._sync_command_exclusion(command_id, resource_id, active=active, reason=reason, until=until)
                        self._conn.execute("COMMIT")
                        return result
                    except BaseException:
                        self._conn.execute("ROLLBACK")
                        raise

    def _exclusion_reservations(self, resource_id):
        return {row[0] for row in self._conn.execute(
            "SELECT reservation_id FROM slot_reservations WHERE state='held' AND "
            "(model=? OR (family=? AND NOT EXISTS (SELECT 1 FROM resources WHERE id=?)))",
            (resource_id, resource_id, resource_id))}

    def _sync_command_exclusion(self, command_id, resource_id, *, active, reason, until):
        with self._lock:
            row = self._conn.execute("SELECT resource_id FROM command_exclusions WHERE command_id=?", (command_id,)).fetchone()
            if row and row["resource_id"] != resource_id:
                raise ValueError("command resource mismatch")
            if active and not self._conn.execute("SELECT 1 FROM resources WHERE id=? OR (kind='model' AND family=?)", (resource_id, resource_id)).fetchone():
                return False
            if active:
                self._conn.execute("""INSERT INTO command_exclusions(command_id, resource_id, reason, until, released)
                    VALUES (?, ?, ?, ?, 0) ON CONFLICT(command_id) DO UPDATE SET
                    reason=excluded.reason, until=excluded.until, released=0""", (command_id, resource_id, reason, until))
            elif row:
                self._conn.execute("UPDATE command_exclusions SET released=1 WHERE command_id=?", (command_id,))
            return True

    def apply_command_exclusions(self, resources: list[dict]) -> list[dict]:
        with self._lock:
            now = timeutil.now_ts()
            ids = {row[0] for row in self._conn.execute(
                "SELECT resource_id FROM command_exclusions WHERE released=0 AND until>? "
                "UNION SELECT resource_id FROM exclusions WHERE released_at IS NULL AND (until IS NULL OR until>?)", (now, now))}
            families = {row[0] for row in self._conn.execute(
                "SELECT c.resource_id FROM command_exclusions c WHERE c.released=0 AND c.until>? "
                "AND NOT EXISTS (SELECT 1 FROM resources WHERE id=c.resource_id)", (now,))}
        return [{**resource, "status": "excluded"} if (resource["id"] in ids or (resource.get("kind") == "model" and resource.get("family") in families)) and resource["status"] in ("active", "degraded")
                else resource for resource in resources]

    # --- 除外（S-8.2）---------------------------------------------------------------------------------
    def exclude(self, resource_id: str, reason: str, until: str | None, *, keep_status: bool = False, now: str | None = None) -> dict | None:
        """期限付きの一時除外を足す。資源が無ければ None。"""
        now = now or timeutil.now_ts()
        with self._lock:
            row = self._conn.execute("SELECT id FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not row:
                return None
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO exclusions(resource_id, reason, until, created_at, keep_status) VALUES (?,?,?,?,?)",
                    (resource_id, reason, until, now, 1 if keep_status else 0),
                )
                if not keep_status:
                    self._conn.execute("UPDATE resources SET status='excluded', updated_at=? WHERE id=? AND status NOT IN ('retired')", (now, resource_id))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            return self.get(resource_id)

    def release_exclusions(self, resource_id: str, *, reason: str | None = None, until: str | None = None, now: str | None = None) -> dict | None:
        """対象の一時除外だけを解除する（S-8.2）。停止・退役や別の除外は解除しない。"""
        now = now or timeutil.now_ts()
        with self._lock:
            row = self._conn.execute("SELECT id, status FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not row:
                return None
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                q = "UPDATE exclusions SET released_at=? WHERE resource_id=? AND released_at IS NULL"
                args: list[Any] = [now, resource_id]
                if reason is not None:
                    q += " AND reason=?"
                    args.append(reason)
                if until is not None:
                    q += " AND until=?"
                    args.append(until)
                self._conn.execute(q, args)
                active_excls = self._conn.execute(
                    "SELECT COUNT(*) FROM exclusions WHERE resource_id=? AND released_at IS NULL", (resource_id,)
                ).fetchone()[0]
                if active_excls == 0 and row["status"] == "excluded":
                    self._conn.execute("UPDATE resources SET status='active', updated_at=? WHERE id=?", (now, resource_id))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            return self.get(resource_id)

    def sweep_expired(self, now: str | None = None) -> list[str]:
        """期限の過ぎた一時除外を解除し、対象の資源の一覧を返す（S-8.2、D-7.3）。"""
        now = now or timeutil.now_ts()
        changed: list[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT resource_id FROM exclusions WHERE released_at IS NULL AND until IS NOT NULL AND until <= ?", (now,)
            ).fetchall()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for (rid,) in rows:
                    self._conn.execute(
                        "UPDATE exclusions SET released_at=? WHERE resource_id=? AND released_at IS NULL AND until IS NOT NULL AND until <= ?",
                        (now, rid, now),
                    )
                    n = self._conn.execute(
                        "SELECT COUNT(*) FROM exclusions WHERE resource_id=? AND released_at IS NULL", (rid,)
                    ).fetchone()[0]
                    if n == 0:
                        self._conn.execute(
                            "UPDATE resources SET status='active', updated_at=? WHERE id=? AND status='excluded'", (now, rid)
                        )
                    changed.append(rid)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return changed

    def active_exclusions(self, resource_id: str, now: str | None = None) -> list[dict]:
        now = now or timeutil.now_ts()
        with self._lock:
            # 期限の過ぎたものは数えない（未解除でも）
            rows = self._conn.execute(
                "SELECT id, resource_id, reason, until, created_at FROM exclusions WHERE resource_id=? AND released_at IS NULL AND (until IS NULL OR until > ?)",
                (resource_id, now),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_status(self, resource_id: str, status: str, *, now: str | None = None) -> dict | None:
        now = now or timeutil.now_ts()
        with self._lock:
            row = self._conn.execute("SELECT id FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not row:
                return None
            self._conn.execute("UPDATE resources SET status=?, updated_at=? WHERE id=?", (status, now, resource_id))
            return self.get(resource_id)

    # --- 成績（S-8.2、S-8.5、D-7.3）-----------------------------------------------------------------
    def record_outcome(self, resource_id: str, *, outcome: str, verify_rc: int | None = None,
                       error_class: str | None = None, counts_as_failure: str | None = None,
                       job_id: str | None = None, trial_id: str | None = None, now: str | None = None) -> dict | None:
        """worker-pool が POST /v1/resources/{id}/outcome で頼む成績の書き込み（D-7.3）。
        利用制限・quota・接続障害・保留は counts_as_failure で能力上の失敗に数えない（S-8.5）。"""
        now = now or timeutil.now_ts()
        with self._lock:
            row = self._conn.execute("SELECT stats FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not row:
                return None
            stats = _loads(row["stats"]) or {}
            trials = stats.get("trials") or []
            trials.append({"trial_id": trial_id, "job_id": job_id, "outcome": outcome, "verify_rc": verify_rc,
                           "error_class": error_class, "counts_as_failure": counts_as_failure, "at": now})
            # 古い物から外す（上限 500 件）
            if len(trials) > 500:
                trials = trials[-500:]
            stats["trials"] = trials
            stats["last_outcome"] = {"outcome": outcome, "verify_rc": verify_rc, "counts_as_failure": counts_as_failure, "at": now}
            self._conn.execute("UPDATE resources SET stats=?, updated_at=? WHERE id=?", (_dumps(stats), now, resource_id))
            return self.get(resource_id)

    def record_evaluated_outcome(self, resource_id: str, *, outcome: str, verify_rc: int | None,
                                 error_class: str | None, counts_as_failure: str,
                                 job_id: str, trial_id: str, level_generation: int | None = None,
                                 promote_after: int = 3, demote_after: int = 2) -> dict | None:
        """Atomically record one job attempt and apply at most one level change."""
        from . import promotion
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                resource = self.get(resource_id)
                if resource is None:
                    self._conn.execute("ROLLBACK")
                    return None
                inserted = self._conn.execute(
                    "INSERT OR IGNORE INTO outcome_receipts(resource_id, trial_id) VALUES (?,?)",
                    (resource_id, trial_id)).rowcount
                if inserted:
                    generation = (resource.get("stats") or {}).get("level_generation", 0)
                    excluded = error_class in ("system.expected", "rate_limit", "quota", "network", "connect", "unavailable")
                    valid = (outcome == "pass" and counts_as_failure == "no" and verify_rc == 0) or (
                        outcome == "fail" and counts_as_failure == "yes" and type(verify_rc) is int and verify_rc != 0)
                    if excluded or not valid or (level_generation is not None and level_generation != generation):
                        counts_as_failure = "hold"
                    self.record_outcome(resource_id, outcome=outcome, verify_rc=verify_rc,
                                        error_class=error_class, counts_as_failure=counts_as_failure,
                                        job_id=job_id, trial_id=trial_id)
                    resource = self.get(resource_id)
                    stats = resource.get("stats") or {}
                    stats["trials"][-1]["level_generation"] = generation
                    self._conn.execute("UPDATE resources SET stats=? WHERE id=?", (_dumps(stats), resource_id))
                    if counts_as_failure in ("yes", "no"):
                        decision = promotion.evaluate(self, resource_id, promote_after=promote_after, demote_after=demote_after, stage_only=True)
                        if decision["new_level"]:
                            stats["level_generation"] = generation + 1
                            self._conn.execute("UPDATE resources SET level=?, stats=?, updated_at=? WHERE id=?",
                                               (decision["new_level"], _dumps(stats), timeutil.now_ts(), resource_id))
                self._conn.execute("COMMIT")
                return self.get(resource_id)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def capability_outcomes(self, resource_id: str, *, since: str | None = None, limit: int = 500) -> list[dict]:
        """能力上の合否だけを返す（利用制限・接続障害・保留は除く）。"""
        with self._lock:
            row = self._conn.execute("SELECT stats FROM resources WHERE id=?", (resource_id,)).fetchone()
        if not row:
            return []
        stats = _loads(row["stats"]) or {}
        trials = stats.get("trials") or []
        out = [t for t in trials if t.get("counts_as_failure") in ("yes", "no")]
        if since:
            out = [t for t in out if t.get("at", "") >= since]
        return out[-limit:]

    # --- 段階の上げ下げ（S-8.5。resources が成績から計算する） --------------------------------------
    def apply_level_change(self, resource_id: str, new_level: str, *, now: str | None = None) -> dict | None:
        now = now or timeutil.now_ts()
        with self._lock:
            row = self._conn.execute("SELECT level, stats FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not row:
                return None
            stats = _loads(row["stats"]) or {}
            if row["level"] != new_level:
                stats["level_generation"] = stats.get("level_generation", 0) + 1
            self._conn.execute("UPDATE resources SET level=?, stats=?, updated_at=? WHERE id=?", (new_level, _dumps(stats), now, resource_id))
            return self.get(resource_id)

    # --- prefix_cache（S-8.12 の 14、D-7.3） ----------------------------------------------------------
    def set_prefix_cache(self, resource_id: str, value: str, *, ratio: float | None = None, measured_at: str | None = None,
                         now: str | None = None) -> dict | None:
        now = now or timeutil.now_ts()
        with self._lock:
            row = self._conn.execute("SELECT stats FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not row:
                return None
            stats = _loads(row["stats"]) or {}
            stats["prefix_cache"] = {"value": value, "ratio": ratio, "measured_at": measured_at, "updated_at": now}
            self._conn.execute("UPDATE resources SET prefix_cache=?, stats=?, updated_at=? WHERE id=?", (value, _dumps(stats), now, resource_id))
            return self.get(resource_id)

    # --- 選択記録（S-8.4 の 6） ----------------------------------------------------------------------
    def record_selection(self, job_id: str, doc: dict, *, now: str | None = None) -> None:
        now = now or timeutil.now_ts()
        with self._lock:
            self._conn.execute("INSERT INTO selections(job_id, created_at, doc) VALUES (?,?,?)", (job_id, now, _dumps(doc)))

    def selections(self, *, job_id: str | None = None, limit: int = 200) -> list[dict]:
        where, args = [], []
        if job_id:
            where.append("job_id=?")
            args.append(job_id)
        sql = "SELECT job_id, created_at, doc FROM selections" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            doc = _loads(r["doc"]) or {}
            doc.setdefault("job_id", r["job_id"])
            doc.setdefault("created_at", r["created_at"])
            out.append(doc)
        return out

    # --- 提供一覧の照合（S-8.2、S-8.8 の /v1/models/discovery） --------------------------------------
    def record_discovery(self, *, ok: bool, provided: list[str] | None = None, error: str = "", now: str | None = None) -> dict:
        now = now or timeutil.now_ts()
        with self._lock:
            if ok:
                provided = provided or []
                registered = [r["id"] for r in self._conn.execute("SELECT id FROM resources").fetchall()]
                new_ids = [p for p in provided if p not in registered]
                matched = [p for p in provided if p in registered]
                unseen = [r for r in registered if r not in provided]
            else:
                previous = self.last_discovery() or {}
                provided = previous.get("provided", [])
                new_ids = previous.get("new_ids", [])
                matched = previous.get("matched", [])
                unseen = previous.get("unseen", [])
            doc = {"fetched_at": now, "ok": ok, "error": error, "provided": provided,
                   "new_ids": new_ids, "matched": matched, "unseen": unseen}
            self._conn.execute("DELETE FROM discovery")
            self._conn.execute(
                "INSERT INTO discovery(fetched_at, ok, error, provided, new_ids, matched, unseen) VALUES (?,?,?,?,?,?,?)",
                (now, 1 if ok else 0, error, _dumps(provided), _dumps(new_ids), _dumps(matched), _dumps(unseen)),
            )
        return doc

    def last_discovery(self) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM discovery LIMIT 1").fetchone()
        if not row:
            return None
        d = dict(row)
        d["ok"] = bool(d["ok"])
        for k in ("provided", "new_ids", "matched", "unseen"):
            d[k] = _loads(d.get(k)) or []
        return d

    # --- 週次確認（S-8.5、S-8.2。昇格の合否に使わない） -----------------------------------------------
    def record_weekly_check(self, model_id: str, *, reachable: bool | None, responded: bool | None,
                            tool_ok: bool | None, diff: dict | None = None, error: str = "", now: str | None = None) -> dict:
        now = now or timeutil.now_ts()
        with self._lock:
            self._conn.execute(
                "INSERT INTO weekly_checks(model_id, ran_at, reachable, responded, tool_ok, diff, error) VALUES (?,?,?,?,?,?,?)",
                (model_id, now, None if reachable is None else 1 if reachable else 0,
                 None if responded is None else 1 if responded else 0, None if tool_ok is None else 1 if tool_ok else 0,
                 _dumps(diff or {}), error),
            )
            prev = self._conn.execute(
                "SELECT reachable, responded, tool_ok FROM weekly_checks WHERE model_id=? AND ran_at < ? ORDER BY ran_at DESC LIMIT 1",
                (model_id, now),
            ).fetchone()
        return {"model_id": model_id, "ran_at": now, "reachable": reachable, "responded": responded, "tool_ok": tool_ok,
                "previous": dict(prev) if prev else None, "diff": diff or {}, "error": error}

    def weekly_checks(self, *, model_id: str | None = None, limit: int = 200) -> list[dict]:
        where, args = [], []
        if model_id:
            where.append("model_id=?")
            args.append(model_id)
        sql = "SELECT * FROM weekly_checks" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ran_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("reachable", "responded", "tool_ok"):
                d[k] = None if d.get(k) is None else bool(d[k])
            d["diff"] = _loads(d.get("diff")) or {}
            out.append(d)
        return out

    # --- 枠の実績の集計（S-8.8 の /v1/ledger） ----------------------------------------------------------
    def ledger(self, *, model: str | None = None, role: str | None = None, since: str | None = None, limit: int = 500) -> list[dict]:
        """モデルごとの実績。ここでは stats の試行一覧から集計し、範囲の外は呼び出し側で絞る。"""
        items = self.list(limit=2000)
        out: list[dict] = []
        for it in items:
            if model and it["id"] != model:
                continue
            trials = (it.get("stats") or {}).get("trials") or []
            for t in trials:
                if since and t.get("at", "") < since:
                    continue
                if role and t.get("role") != role:
                    continue
                out.append({"model": it["id"], "job_id": t.get("job_id"), "trial_id": t.get("trial_id"),
                            "outcome": t.get("outcome"), "verify_rc": t.get("verify_rc"),
                            "counts_as_failure": t.get("counts_as_failure"), "at": t.get("at")})
        return out[-limit:]
