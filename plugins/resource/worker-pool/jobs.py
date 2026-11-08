"""ジョブの保存（SQLite）。状態と試行の記録を持つ（D-7.1、S-8.6）。

jobs 表は、selector（1 秒ごと）と supervisor と finisher が共有する。状態は
job.state に記録する（queued / launching / submitted / running / stalled / ok / failed /
timed_out / cancelled / lost / rejected）。立つ作業は jobs の launching・submitted・running
を数えて枠を数え出す（D-7.4。別の数え方の表は持たない）。

attempts 表は各試行の attempt_id、モデル、開始・終了時刻、結果、検証結果、切替理由を対応付ける。
usage の値は worker.usage の事象と試行の両方に記録する（S-8.6、S-8.12 の 12）。

秘密の値は持たない。依頼本文は持たない（間引き結果の report のみ）。
"""
from __future__ import annotations

import json
import fcntl
import os
from contextlib import contextmanager
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from axcommon import timeutil

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  prompt TEXT NOT NULL DEFAULT '',
  sensitivity TEXT NOT NULL DEFAULT 'public',
  needs_vision INTEGER NOT NULL DEFAULT 0,
  project TEXT,
  subloop TEXT,
  item_id TEXT,
  todo_id TEXT,
  state TEXT NOT NULL DEFAULT 'queued',
  model TEXT,
  workdir_id TEXT,
  session_id TEXT,
  resume_from TEXT,
  resume_required INTEGER NOT NULL DEFAULT 0,
  root TEXT,
  chain_length INTEGER NOT NULL DEFAULT 0,
  last_context INTEGER,
  placement TEXT NOT NULL DEFAULT 'local',
  tools TEXT NOT NULL DEFAULT '[]',
  budget_min INTEGER,
  deadline TEXT,
  reason TEXT NOT NULL DEFAULT '',
  verify TEXT NOT NULL DEFAULT '',
  attachments TEXT NOT NULL DEFAULT '[]',
  depends TEXT NOT NULL DEFAULT '[]',
  request TEXT NOT NULL DEFAULT '',
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  slot_released INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  ended_at TEXT,
  prefix_id TEXT NOT NULL DEFAULT '',
  report_status TEXT,
  report_text TEXT NOT NULL DEFAULT '',
  human_action TEXT NOT NULL DEFAULT '',
  verify_rc INTEGER,
  verify_log_tail TEXT NOT NULL DEFAULT '',
  changed_files TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS jobs_role ON jobs(role, state);
CREATE INDEX IF NOT EXISTS jobs_resume ON jobs(resume_from);
CREATE INDEX IF NOT EXISTS jobs_root ON jobs(root);
CREATE TABLE IF NOT EXISTS verification_recipes (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  request TEXT NOT NULL,
  snapshot_id TEXT UNIQUE,
  response TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification_jobs (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  request TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_no INTEGER NOT NULL,
  job_id TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'running',
  started_at TEXT NOT NULL,
  ended_at TEXT,
  result TEXT NOT NULL DEFAULT '',
  verify_rc INTEGER,
  counts_as_failure TEXT,
  switch_reason TEXT NOT NULL DEFAULT '',
  usage TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS attempts_job ON attempts(job_id);
"""

JOB_ID_RE = __import__("re").compile(r"^[a-z0-9-]{1,40}$")
TERMINAL = ("ok", "failed", "timed_out", "cancelled", "lost", "rejected")


def new_job_id() -> str:
    return "job-" + uuid.uuid4().hex[:24]


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _loads(text: str, default: Any = None) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


class JobsStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.launch_lock_path = os.environ.get("AX_WORKER_POOL_LOCK_DB") or self.path
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._launch_locks = {}
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        if "slot_released" not in columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN slot_released INTEGER NOT NULL DEFAULT 0")
        if "family" not in columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN family TEXT")
        if "tool_free_evidence" not in columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN tool_free_evidence TEXT")
        if "tool_free" not in columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN tool_free INTEGER")
        if "idempotency_key" not in columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN idempotency_key TEXT")
        if "request_digest" not in columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN request_digest TEXT")
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL")
        attempt_columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(attempts)")}
        if "runner" not in attempt_columns:
            self._conn.execute("ALTER TABLE attempts ADD COLUMN runner TEXT")
        if "family" not in attempt_columns:
            self._conn.execute("ALTER TABLE attempts ADD COLUMN family TEXT")
        if "launched" not in attempt_columns:
            self._conn.execute("ALTER TABLE attempts ADD COLUMN launched INTEGER NOT NULL DEFAULT 0")
        if "level_generation" not in attempt_columns:
            self._conn.execute("ALTER TABLE attempts ADD COLUMN level_generation INTEGER")
        if "outcome_sent" not in attempt_columns:
            self._conn.execute("ALTER TABLE attempts ADD COLUMN outcome_sent INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- 作成 ------------------------------------------------------------------------------------------
    def create(self, req: dict, *, session_id: str | None = None, prefix: str = "",
               root: str | None = None, chain_length: int = 0, now: str | None = None) -> dict:
        now = now or timeutil.now_ts()
        jid = req.get("id") or new_job_id()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(id, role, prompt, sensitivity, needs_vision, project, subloop, item_id, todo_id, state,"
                " workdir_id, session_id, resume_from, resume_required, root, chain_length, placement, tools, budget_min, deadline,"
                " verify, attachments, depends, request, created_at, prefix_id, idempotency_key, request_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (jid, req["role"], req.get("prompt", ""), req.get("sensitivity", "public"), 1 if req.get("needs_vision") else 0,
                 req.get("project"), req.get("subloop"), req.get("item_id"), req.get("todo_id"), req.get("state", "queued"),
                 req.get("workdir_id"), session_id, req.get("resume_from"), 1 if req.get("resume_required") else 0,
                 root or req.get("root"), chain_length, req.get("placement", "local"), _dumps(req.get("tools") or []),
                 req.get("budget_min"), req.get("deadline"), (req["verify"].get("cmd", "") if isinstance(req.get("verify"), dict) else req.get("verify") or ""), _dumps(req.get("attachments") or []),
                 _dumps(req.get("depends_on") or []), _dumps(req), now, prefix, req.get("idempotency_key"), req.get("_request_digest")),
            )
        return self.get(jid) or {}

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("tools", "attachments", "depends", "request", "changed_files", "tool_free_evidence"):
            d[k] = _loads(d.get(k)) or []
        d["needs_vision"] = bool(d.get("needs_vision"))
        d["resume_required"] = bool(d.get("resume_required"))
        return d

    def list(self, *, state: str | None = None, role: str | None = None, limit: int = 200) -> list[dict]:
        where, args = [], []
        if state:
            where.append("state=?")
            args.append(state)
        if role:
            where.append("role=?")
            args.append(role)
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self.get(r["id"]) for r in rows]

    # --- 状態 ------------------------------------------------------------------------------------------
    def set_state(self, job_id: str, state: str, *, reason: str = "", model: str | None = None,
                  started_at: str | None = None, finished_at: str | None = None, now: str | None = None) -> dict | None:
        now = now or timeutil.now_ts()
        with self._lock:
            col = []
            args: list[Any] = []
            for k, v in (("state", state), ("reason", reason), ("model", model), ("started_at", started_at), ("finished_at", finished_at)):
                if v is not None:
                    col.append(f"{k}=?")
                    args.append(v)
            if state in TERMINAL:
                col.append("finished_at=?")
                args.append(finished_at or now)
                col.append("ended_at=?")
                args.append(finished_at or now)
            if not col:
                return self.get(job_id)
            args.append(job_id)
            self._conn.execute("UPDATE jobs SET " + ", ".join(col) + " WHERE id=?", args)
        return self.get(job_id)

    def update_session(self, job_id: str, session_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE jobs SET session_id=? WHERE id=?", (session_id, job_id))

    def record_result(self, job_id: str, *, status: str, text: str = "", human_action: str = "",
                      verify_rc: int | None = None, verify_log_tail: str = "", changed_files: list | None = None) -> dict | None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET report_status=?, report_text=?, human_action=?, verify_rc=?, verify_log_tail=?, changed_files=? WHERE id=?",
                (status, text, human_action, verify_rc, verify_log_tail, _dumps(changed_files or []), job_id),
            )
        return self.get(job_id)

    def slot_usage(self) -> dict:
        """枠の数え方（D-7.4）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT model, family_dummy, state FROM jobs WHERE state IN ('launching','submitted','running')"
            ).fetchall() if False else self._conn.execute(
                "SELECT model FROM jobs WHERE state IN ('launching','submitted','running')"
            ).fetchall()
        return {"in_use": len(rows)}

    def queue(self, role: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM jobs WHERE role=? AND state='queued' ORDER BY created_at", (role,)).fetchall()
        return [self.get(r["id"]) for r in rows]

    # --- 試行 ------------------------------------------------------------------------------------------
    def start_attempt(self, job_id: str, model: str, *, attempt_no: int = 1, now: str | None = None, level_generation: int | None = None, family: str | None = None, runner: str | None = None) -> int:
        now = now or timeutil.now_ts()
        if not JOB_ID_RE.match(job_id):
            raise ValueError("job_id が不正")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO attempts(attempt_no, job_id, model, started_at, level_generation, family, runner) VALUES (?,?,?,?,?,?,?)", (attempt_no, job_id, model, now, level_generation, family, runner)
            )
            return int(cur.lastrowid)

    def confirm_launch(self, job_id: str, attempt_id: int) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("UPDATE attempts SET launched=1 WHERE id=? AND job_id=?", (attempt_id, job_id))
                self._conn.execute("UPDATE jobs SET family=(SELECT family FROM attempts WHERE id=? AND job_id=?) WHERE id=?", (attempt_id, job_id, job_id))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def prior_execution(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT model, family FROM attempts WHERE job_id=? AND launched=1 ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        if row and row["model"] and isinstance(row["family"], str) and row["family"].strip():
            return dict(row)
        return None

    def finish_attempt(self, attempt_id: int, *, state: str, result: str = "", verify_rc: int | None = None,
                       counts_as_failure: str | None = None, switch_reason: str = "", usage: dict | None = None,
                       now: str | None = None) -> None:
        now = now or timeutil.now_ts()
        with self._lock:
            self._conn.execute(
                "UPDATE attempts SET state=?, result=?, verify_rc=?, counts_as_failure=?, switch_reason=?, usage=?, ended_at=? WHERE id=?",
                (state, result, verify_rc, counts_as_failure, switch_reason, _dumps(usage or {}), now, attempt_id),
            )

    def attempts(self, job_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY attempt_no", (job_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["usage"] = _loads(d.get("usage")) or {}
            out.append(d)
        return out

    @contextmanager
    def launch_lock(self, job_id: str):
        # Serialize cancellation with the actual start, across threads and processes.
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("invalid job id")
        with self._lock:
            lock = self._launch_locks.setdefault(job_id, threading.Lock())
        with lock:
            fd = None
            try:
                if self.path != ":memory:":
                    fd = os.open(self.launch_lock_path + ".launch-" + job_id + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
                    os.fchmod(fd, 0o644)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                if fd is not None:
                    os.close(fd)

    def cancel(self, job_id: str) -> dict | None:
        if not JOB_ID_RE.fullmatch(job_id):
            return None
        with self.launch_lock(job_id), self._lock:
            row = self._conn.execute("SELECT id, state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            if row["state"] in TERMINAL:
                return self.get(job_id)
            if row["state"] in ("queued", "reserving"):
                now = timeutil.now_ts()
                self._conn.execute(
                    "UPDATE jobs SET cancel_requested=1, state='cancelled', reason='cancelled', "
                    "finished_at=?, ended_at=? WHERE id=? AND state IN ('queued','reserving')",
                    (now, now, job_id),
                )
            else:
                self._conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
            return self.get(job_id)

# --- POST /v1/jobs の実体（starlette を経由しない層。S-8.8） --------------------------------
WORKDIR_RE = __import__("re").compile(r"^[a-z0-9-]{1,40}$")
ROLES = ("summary", "research", "second_opinion", "screen", "assist", "test_design", "impl",
         "impl_hard", "review", "review_final", "manager", "backup_manager", "impl_public")


class ValidationError(ValueError):
    pass


def validate_job_body(body: Any) -> dict:
    if not isinstance(body, dict):
        raise ValidationError("本文が JSON のオブジェクトでない")
    if "pin_model" in body:
        raise ValidationError("pin_model_not_allowed")
    if "session_id" in body:
        raise ValidationError("session_id_not_allowed")
    for k in ("role", "prompt", "sensitivity"):
        if k not in body:
            raise ValidationError(f"{k} が無い")
    if body["role"] not in ROLES:
        raise ValidationError("role が不正")
    if body["sensitivity"] not in ("public", "private", "sensitive"):
        raise ValidationError("sensitivity が不正")
    wd = body.get("workdir_id")
    if wd is not None and not WORKDIR_RE.match(str(wd)):
        raise ValidationError("workdir_id が不正")
    tools = body.get("tools") or []
    if tools:
        if body["role"] not in ("research", "screen", "assist"):
            raise ValidationError("tools は research / screen / assist だけ持てる")
        if body["sensitivity"] != "public":
            raise ValidationError("tools は sensitivity public のときだけ持てる")
        for t in tools:
            if t not in ("sandbox-desktop", "web-gui", "web-screenshot", "linux-sandbox"):
                raise ValidationError("tools の能力名が不正")
    if body.get("resume_from") and wd and not isinstance(body["resume_from"], str):
        raise ValidationError("resume_from が不正")
    return body


def submit_internal(store: "JobsStore", body: dict, *, pin_model: str | None = None,
                    session_id: str | None = None, prefix: str = "", root: str | None = None,
                    chain_length: int = 0) -> dict:
    req = dict(body)
    job = store.create(req, session_id=session_id or req.get("session_id") or str(uuid.uuid4()),
                       prefix=prefix, root=root, chain_length=chain_length)
    if pin_model:
        job["pin_model"] = pin_model
    return job
