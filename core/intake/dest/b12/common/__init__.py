"""S-7.10 delivery, timeline subscription and F-18.3 acceptance."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import threading
from datetime import timedelta
from pathlib import Path

from axcommon import creds, ids, schemas, timeutil
from axcommon.client import ApiError, ControlClient, LedgerClient, TimelineClient, WorkerPoolClient, service_url
from .backend import MissingBackend
from .store import Store

log = logging.getLogger("axintake.b12")

READ_ROLES = frozenset({"summary", "research", "second_opinion", "screen", "assist"})


def fault(status, code):
    return ApiError(status, code, code)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Engine:
    def __init__(self, app, *, store=None, backend=None, deploy=None):
        self.app, self.store = app, store
        transport = app.state.intake.transport
        self.ledger = LedgerClient(service_url("ledger"), transport=transport)
        self.control = ControlClient(service_url("control"), transport=transport)
        self.pool = WorkerPoolClient(service_url("worker-pool"), token=creds.read_secret_optional("core-token-intake"), transport=transport)
        self.timeline = TimelineClient(service_url("timeline"), transport=transport)
        self.backend = backend or MissingBackend()
        self.deploy = deploy or self._deploy
        self.lock = threading.RLock()
        self.accept_lock = threading.RLock()
        self.finalizers = {}

    @property
    def db(self):
        with self.lock:
            if self.store is None:
                self.store = Store(Path(os.environ.get("AX_STATE_DIR", "/state")) / "intake/b12.sqlite3")
            return self.store

    def _deploy(self, key):
        # B8 format, with a stable request ID persisted before publishing.
        rec = self.record(key)
        if not rec.get("deploy_id"):
            rec["deploy_id"] = ids.new_ulid()
            self.save(rec)
        directory = Path(os.environ.get("AX_STATE_DIR", "/state")) / "host-requests"
        directory.mkdir(parents=True, exist_ok=True)
        name = rec["deploy_id"] + ".req"
        final = directory / name
        if any((directory / bucket / name).exists() for bucket in (".", "done", "rejected")):
            return str(final)
        temporary = directory / ("." + rec["deploy_id"] + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write('{"op":"deploy"}\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, final)
        return str(final)

    def save(self, rec):
        self.db.put("item:" + rec["item"]["item_key"], rec)

    def record(self, key):
        rec = self.db.get("item:" + key)
        if rec is None:
            raise fault(404, "item_not_found")
        return rec

    def approved(self, approval_id, *, payload=None):
        approvals = getattr(self.app.state, "approvals", None)
        if not approval_id or approvals is None:
            raise fault(403, "approval_required")
        row = approvals.store.get(approval_id)
        if (not row or row["destination"] != "core.change" or row["state"] not in {"granted", "executed"}
                or not row.get("granted_at")):
            raise fault(403, "approval_required")
        from core.intake.approval.store import payload_digest
        approved_payload = json.loads(row["payload_json"])
        if row["digest"] != payload_digest(approved_payload):
            raise fault(403, "approval_payload_mismatch")
        if payload is not None and canonical(approved_payload) != canonical(payload):
            raise fault(403, "approval_payload_mismatch")
        return row

    def ensure_todo(self, item):
        todo = {"id": item["todo_id"], "title": item["title"], "project": item["project"],
                "owner": "system", "state": "todo", "source": {"item_id": item["item_key"]}}
        try:
            self.ledger.create_todo(todo)
        except ApiError as exc:
            if exc.status != 409 or exc.code != "already_exists":
                raise
            # A pre-existing ID must belong to this item, never adopt an arbitrary ToDo.
            existing = self.ledger.get_todo(item["todo_id"])
            existing = existing.get("data", existing)
            if (existing.get("source", {}).get("item_id") != item["item_key"]
                    or existing.get("project") != item["project"] or existing.get("title") != item["title"]):
                raise fault(409, "todo_mismatch") from None

    def transition(self, rec, state, reason=None):
        extra = {}
        if state == "parked":
            extra["check_at"] = timeutil.format_ts(timeutil.now() + timedelta(hours=1))
        todo = self.ledger.get_todo(rec["item"]["todo_id"])
        todo = todo.get("data", todo)
        if todo.get("state") == state:
            return
        self.ledger.transition(rec["item"]["todo_id"], state, reason, **extra)

    def park(self, rec, reason):
        rec["reason"] = reason
        self.save(rec)
        self.transition(rec, "parked", reason)

    def submit(self, item, *, approval_id=None, metadata=None):
        item = copy.deepcopy(item)
        schemas.validate("work-item", item)
        if item["item_key"] != item["todo_id"]:
            raise fault(422, "item_key_must_be_todo_id")
        if item.get("base_ref", "main") != "main":
            raise fault(422, "base_ref_must_be_main")
        item.setdefault("base_ref", "main")
        if item["kind"] == "work":
            item.setdefault("branch", "ai/" + item["item_key"])
        elif not item.get("branch", "").startswith({"core_change": "core/", "subloop_create": "subloop/"}[item["kind"]]):
            raise fault(422, "branch_kind_mismatch")
        if item["kind"] == "core_change":
            self.approved(approval_id)
        with self.lock:
            rec = self.db.get("item:" + item["item_key"])
            if rec:
                if canonical(rec["original"]) != canonical(item) or rec.get("approval_id") != approval_id:
                    raise fault(409, "idempotency_mismatch")
                if rec["phase"] != "created":
                    if rec.get("ledger_pending"):
                        self.transition(rec, "running")
                        rec["ledger_pending"] = False
                        self.save(rec)
                    return {"todo_id": item["todo_id"], "item_key": item["item_key"], "duplicate": True, "phase": rec["phase"]}
            else:
                rec = {"item": item, "original": copy.deepcopy(item), "approval_id": approval_id,
                       "metadata": metadata or {}, "phase": "created"}
                self.save(rec)  # persist before any external side effect
            self.ensure_todo(item)
            return self.deliver(rec)

    def deliver(self, rec):
        try:
            result = self.control.post("/v1/plugins/work-tdd/items", rec["item"])
        except ApiError as exc:
            if exc.status == 429 and exc.code == "inbox_full":
                rec["phase"] = "inbox_full"
                self.park(rec, "inbox_full")
                return {"todo_id": rec["item"]["todo_id"], "state": "parked", "reason": "inbox_full"}
            raise
        rec["phase"] = "submitted"
        rec["ledger_pending"] = True
        self.save(rec)
        self.transition(rec, "running")
        rec["ledger_pending"] = False
        self.save(rec)
        return {"todo_id": rec["item"]["todo_id"], "item_key": rec["item"]["item_key"], "result": result}

    def accept(self, key, *, before_accept=None):
        with self.accept_lock:
            rec = self.record(key)
            if rec["item"]["kind"] == "work":
                return {"state": "review", "todo_id": key}  # ledger consumes ordinary completions
            if rec["phase"] == "done":
                return {"state": "done", "sha": rec["sha"], "duplicate": True}
            if not rec.get("completed"):
                raise fault(409, "completion_required")
            stage = "approval"
            try:
                if rec["item"]["kind"] == "core_change":
                    self.approved(rec.get("approval_id"))
                if "sha" not in rec:
                    stage = "snapshot"
                    rec["sha"] = self.backend.snapshot(rec["item"]["branch"])
                    rec["phase"] = "snapshot"
                    self.save(rec)
                if rec["phase"] == "snapshot":
                    stage = "pre_accept"
                    check = before_accept or self.finalizers.get(rec["item"]["kind"])
                    if rec["item"]["kind"] == "subloop_create" and check is None:
                        raise NotImplementedError("S-6.21: subloop pre-registration check required")
                    if check is not None and check(rec) is not True:
                        raise fault(422, "manifest_exceeds_declaration")
                    stage = "verify"
                    if self.backend.verify(rec["sha"]) is not True:
                        raise fault(422, "verify_failed")
                    rec["phase"] = "verified"
                    self.save(rec)
                if rec["phase"] == "verified":
                    stage = "merge"
                    if rec["item"]["kind"] == "core_change":
                        self.approved(rec.get("approval_id"))
                    if self.backend.merge(rec["sha"]) != rec["sha"]:
                        raise fault(409, "merge_sha_mismatch")
                    rec["phase"] = "merged"
                    self.save(rec)
                if rec["phase"] == "merged":
                    stage = "tag"
                    self.backend.tag(rec["sha"])
                    rec["phase"] = "tagged"
                    self.save(rec)
                if rec["phase"] == "tagged":
                    stage = "deploy"
                    if rec["item"]["kind"] == "core_change":
                        self.approved(rec.get("approval_id"))
                    rec.setdefault("deploy_id", ids.new_ulid())
                    self.save(rec)
                    rec["deploy_request"] = self.deploy(key)
                    rec["phase"] = "deployed"
                    self.save(rec)
                stage = "ledger"
                self.transition(rec, "done")
                rec["phase"] = "done"
                rec.pop("reason", None)
                self.save(rec)
                return {"state": "done", "sha": rec["sha"]}
            except Exception as exc:
                reason = exc.code if isinstance(exc, ApiError) else stage + "_unavailable"
                self.park(rec, reason)
                return {"state": "parked", "reason": reason, "branch": rec["item"]["branch"]}

    def read_history(self, todo_id):
        from urllib.parse import quote
        todo = self.ledger.get('/v1/todos/' + quote(todo_id, safe=''), params={'history': '1'})
        todo = todo.get('data', todo)
        history = todo.get('history')
        if not isinstance(history, list) or not history or type(history[-1].get('seq')) is not int:
            raise fault(503, 'todo_history_unavailable')
        return todo, history

    def start_read(self, key, rec):
        if rec.get('settled') or not rec.get('job_id') or type(rec.get('initial_seq')) is not int:
            return
        marker = 'read-job:' + rec['job_id']
        todo, history = self.read_history(rec['todo_id'])
        if not rec.get('running_seq'):
            matches = [row for row in history if row.get('seq', 0) > rec.get('initial_seq', 0)
                       and row.get('kind') == 'transition:todo->running'
                       and row.get('actor') == 'intake'
                       and (row.get('after') or {}).get('reason') == marker]
            if matches:
                rec['running_seq'] = matches[0]['seq']
                self.db.put(key, rec)
            elif (history[-1]['seq'] == rec.get('initial_seq') and todo.get('state') == 'todo'):
                try:
                    self.ledger.transition(rec['todo_id'], 'running', marker,
                                           expected_history_seq=rec['initial_seq'])
                except ApiError as exc:
                    if exc.status == 409:
                        current, current_history = self.read_history(rec['todo_id'])
                        if current.get('state') != 'todo' or current_history[-1]['seq'] != rec['initial_seq']:
                            rec['settled'] = True
                            self.db.put(key, rec)
                            return
                    raise
                todo, history = self.read_history(rec['todo_id'])
                matches = [row for row in history if row.get('seq', 0) > rec['initial_seq']
                           and row.get('kind') == 'transition:todo->running'
                           and row.get('actor') == 'intake'
                           and (row.get('after') or {}).get('reason') == marker]
                if not matches:
                    raise fault(503, 'todo_history_unavailable')
                rec['running_seq'] = matches[0]['seq']
                self.db.put(key, rec)
            else:
                rec['settled'] = True
                self.db.put(key, rec)

    def reconcile_read(self, key, rec):
        with self.lock:
            rec = self.db.get(key)
            if not rec or rec.get('settled') or not rec.get('job_id'):
                return
            self.start_read(key, rec)
            rec = self.db.get(key)
            if rec.get('settled') or not rec.get('running_seq'):
                return
            job = self.pool.job(rec['job_id'])
            if not isinstance(job, dict) or job.get('id') != rec['job_id']:
                return
            state = job.get('state')
            if state not in {'ok', 'failed', 'timed_out', 'cancelled', 'lost', 'rejected'}:
                return
            report = job.get('report') if isinstance(job.get('report'), dict) else {}
            verify = job.get('verify') if isinstance(job.get('verify'), dict) else {}
            passed = (state == 'ok' and report.get('status') == 'ready'
                      and type(verify.get('rc')) is int and verify['rc'] == 0)
            todo, history = self.read_history(rec['todo_id'])
            if todo.get('state') != 'running' or history[-1]['seq'] != rec['running_seq']:
                rec['settled'] = True
                self.db.put(key, rec)
                return
            if state == 'ok' and not passed:
                reason = 'report_not_ready' if report.get('status') != 'ready' else 'verification_missing_or_failed'
            else:
                reason = str(job.get('reason') or state)[:200]
            extra = {'expected_history_seq': rec['running_seq']}
            if not passed:
                extra['check_at'] = timeutil.format_ts(timeutil.now() + timedelta(hours=1))
            try:
                self.ledger.transition(rec['todo_id'], 'review' if passed else 'parked',
                                       None if passed else reason, **extra)
            except ApiError as exc:
                if exc.status != 409:
                    raise
                current, current_history = self.read_history(rec['todo_id'])
                if current.get('state') == 'running' and current_history[-1]['seq'] == rec['running_seq']:
                    raise
            rec['settled'] = True
            self.db.put(key, rec)

    def event(self, event):
        data = event.get("data") or {}
        source = event.get("source") or {}
        if not isinstance(data, dict) or not isinstance(source, dict):
            return
        if event.get("kind") == "job.state":
            if source.get("component") != "worker-pool":
                return
            job_id = data.get("job_id")
            if not isinstance(job_id, str):
                return
            for key, rec in self.db.read_requests():
                if rec.get("job_id") == job_id:
                    try:
                        self.reconcile_read(key, rec)
                    except Exception as exc:
                        log.warning("read job event reconciliation unavailable: %s", type(exc).__name__)
            return
        if event.get("kind") == "item.state":
            if source.get("component") != "work-tdd":
                return
            key = data.get("item_id")
            rec = self.db.get("item:" + str(key))
            if not rec or data.get("todo_id") != rec["item"]["todo_id"] or data.get("state") != "completed":
                return
            if (rec["item"]["kind"] == "work" or rec["phase"] == "done"
                    or rec.get("completed") and rec.get("reason")):
                return
            rec["completed"] = True
            self.save(rec)
            self.accept(key)
        elif event.get("kind") == "task.state" and source.get("component") == "ledger" and data.get("state") == "todo":
            # Only a user answer resumes a waiting item. Other todo transitions
            # are retries for parked delivery/acceptance, never a fresh answer.
            key = data.get("todo_id")
            rec = self.db.get("item:" + str(key))
            if not rec or rec["phase"] == "done":
                return
            todo = self.ledger.get_todo(key)
            todo = todo.get("data", todo)
            if todo.get("state") != "todo":
                return
            answer = (todo.get("card") or {}).get("answer")
            if data.get("reason") == "answered" and todo.get("resume_ref") and isinstance(answer, dict) and isinstance(answer.get("answer"), str):
                rec["item"].update(resume_ref=todo["resume_ref"], answer=answer["answer"])
                self.save(rec)
                self.deliver(rec)
            elif rec.get("completed"):
                self.transition(rec, "running")
                self.accept(key)
            elif rec["phase"] == "inbox_full":
                self.deliver(rec)

    def poll(self):
        # Separate cursors prevent a long-poll on one kind from losing the other.
        for kind in ("item.state", "task.state", "job.state"):
            with self.lock:
                cursor = self.db.get("cursor:" + kind) or 0
            page = self.timeline.get_events(kind=kind, after=cursor, wait=0, limit=100)
            for event in page["events"]:
                self.event(event)
                self.db.put("cursor:" + kind, event["seq"])
            with self.lock:
                self.db.put("cursor:" + kind, page["next_after"])
        for key, rec in self.db.read_requests():
            if rec.get("job_id") and not rec.get("settled"):
                try:
                    self.reconcile_read(key, rec)
                except Exception as exc:
                    log.warning("read job reconciliation unavailable: %s", type(exc).__name__)


def engine(app):
    return app.state.b12


def submit_item(app, item, *, approval_id=None, metadata=None):
    return engine(app).submit(item, approval_id=approval_id, metadata=metadata)


def accept_branch(app, item_key, *, before_accept=None):
    return engine(app).accept(item_key, before_accept=before_accept)


async def watch(app, stopping):
    while not stopping.is_set():
        try:
            await asyncio.to_thread(engine(app).poll)
        except Exception:
            # A page is replayed on failure; other destinations keep running.
            pass
        try:
            await asyncio.wait_for(stopping.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


def register(app):
    if getattr(app.state, "b12", None) is not None:
        return
    app.state.b12 = Engine(app)
    old_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(application):
        async with old_lifespan(application):
            stopping = asyncio.Event()
            task = asyncio.create_task(watch(application, stopping))
            try:
                yield
            finally:
                stopping.set()
                await task
                if engine(application).store is not None:
                    engine(application).store.close()
    app.router.lifespan_context = lifespan
