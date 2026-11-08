"""worker-pool の API（S-8.8、D-7.1）。

  POST /v1/jobs                        依頼。pin_model を含む本文は 400（pin_model_not_allowed）で拒否する
  GET  /v1/jobs/{id}                   状態と結果
  GET  /v1/jobs?state=&role=&limit=    待機・実行・完了済みの一覧
  POST /v1/jobs/{id}/cancel            指定ジョブだけを取消
  GET  /v1/slots                       系統・モデルごとの使用数と上限
  GET  /v1/ledger                      モデルごとの実績（resources の API を呼ぶ）
  GET  /v1/models/discovery            提供一覧と登録簿の差（resources の API を呼ぶ）
  GET  /v1/selections                  選択記録、候補、除外理由、点、枠、選択理由（resources の API を呼ぶ）
  GET  /v1/child-evaluations           子の評価と親の検証を区別して返す
  GET / POST /status と /apply         S-4.4

内部の関数 submit_internal だけが pin_model を持てる（HTTP の API ではない）。
内部の関数を外から呼ぶ経路は無い。構築者の内部コマンド ax-wp-probe-prefix だけが同じ内部の関数を呼ぶ。

launcher は SSH で呼ぶクライアントとして作る（S-8.11）。このファイルはその HTTP の面だけ。
"""
from __future__ import annotations

import logging
import asyncio
import hashlib
import json
import hmac
import threading
from copy import deepcopy
import re
import uuid
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon import timeutil
from axcommon.http import json_error

from . import COMPONENT, VERSION
from .jobs import JobsStore

log = logging.getLogger("worker-pool")

WORKDIR_RE = re.compile(r"^[a-z0-9-]{1,40}$")
ROLES = ("summary", "research", "second_opinion", "screen", "assist", "test_design", "impl",
         "impl_hard", "review", "review_final", "manager", "backup_manager", "impl_public")


class PoolService:
    def __init__(self, store: JobsStore, *, host: str = "unknown", resources=None, emit=None):
        self.store = store
        self.host = host
        self.resources = resources
        self._emit = emit or (lambda *args, **kwargs: None)
        self.drain_started_at = None
        self.drain_last_rowid = None
        self._settings_lock = threading.RLock()
        self._settings = {"mode": "poll_paused", "knobs": {}, "revision": -1}

    def apply_effective(self, body):
        if (not isinstance(body, dict) or body.get("mode") not in ("run", "collect_only", "poll_paused", "draining")
                or not isinstance(body.get("knobs"), dict) or type(body.get("revision")) is not int):
            raise ValueError("invalid_effective")
        with self._settings_lock:
            if body["revision"] < self._settings["revision"]:
                return False
            if body["mode"] == "draining" and self._settings["mode"] != "draining":
                with self.store._lock:
                    self.drain_started_at = timeutil.now().isoformat(timespec="microseconds").replace("+00:00", "Z")
                    self.drain_last_rowid = self.store._conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM jobs").fetchone()[0]
            elif body["mode"] != "draining":
                self.drain_started_at = None
                self.drain_last_rowid = None
            self._settings = deepcopy({k: body[k] for k in ("mode", "knobs", "revision")})
            return True

    def emit(self, kind, job, data):
        try:
            self._emit(kind, job["id"], data, project=job.get("project"), subloop=job.get("subloop"))
        except Exception as exc:
            log.warning("event %s failed: %s", kind, type(exc).__name__)

    def selection_knobs(self):
        from .selection import Knobs
        with self._settings_lock:
            values = self._settings["knobs"]
            fields = Knobs.__dataclass_fields__
            aliases = {"promotion_passes": "promote_after", "demotion_failures": "demote_after"}
            return Knobs(**{aliases.get(k, k): deepcopy(v) for k, v in values.items()
                            if aliases.get(k, k) in fields
                            and (k not in {"role_candidates", "family_cap", "model_cap"} or isinstance(v, dict))})

    def bind_loop(self):
        return None

    @property
    def sink(self):
        return None


def _err(status: int, code: str, message: str = "") -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class ValidationError(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


def validate_job_body(body: Any) -> dict:
    """POST /v1/jobs の本文の検査（S-8.6、S-8.8）。"""
    if not isinstance(body, dict):
        raise ValidationError("本文が JSON のオブジェクトでない")
    if "pin_model" in body:
        # HTTP も MCP も 400（pin_model_not_allowed）で拒否し、ジョブを作らない（S-8.8）
        raise ValidationError("pin_model_not_allowed")
    if "session_id" in body:
        raise ValidationError("session_id_not_allowed")
    from axcommon import schemas
    if schemas.errors("job-request", body):
        raise ValidationError("job_request_schema")
    for k in ("role", "prompt", "sensitivity"):
        if k not in body:
            raise ValidationError(f"{k} が無い")
    if body["role"] not in ROLES:
        raise ValidationError("role が不正")
    if body["sensitivity"] not in ("public", "private", "sensitive"):
        raise ValidationError("sensitivity が不正")
    fixed_roles = {"ax:change-safety-review-v1": "second_opinion", "ax:change-injection-screen-v1": "screen"}
    if isinstance(body.get("verify"), str) and body["verify"] in fixed_roles and body["role"] != fixed_roles[body["verify"]]:
        raise ValidationError("invalid_fixed_profile_role")
    if isinstance(body.get("verify"), str) and body["verify"] in fixed_roles:
        if any(body.get(k) for k in ("tools", "attachments", "project", "workdir_id", "resume_from", "read_input")):
            raise ValidationError("fixed_profile_extra_input_not_allowed")
    if body.get("verify") == "ax:change-injection-screen-v1" and not body.get("prior_job_id"):
        raise ValidationError("screen_prior_required")
    read_input = body.get("read_input")
    if read_input is not None:
        if (body.get("verify") != "ax:research-report-v1" or not body.get("project")
                or body["role"] not in ("research", "summary", "second_opinion", "screen", "assist")
                or body.get("workdir_id") or body.get("resume_from") or body.get("tools")):
            raise ValidationError("invalid_read_input")
    elif body.get("verify") == "ax:research-report-v1":
        raise ValidationError("missing_read_input")
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
    if body.get("resume_from") and wd:
        # resume_from と workdir_id を両方渡すときは同じ場所でなければならない（S-8.6）
        # ここでは jobs 表の元の wd との照合は submit 側で seats の判定として扱う。形だけ確かめる
        if not isinstance(body["resume_from"], str):
            raise ValidationError("resume_from が不正")
    return body


def submit_internal(store: JobsStore, body: dict, *, pin_model: str | None = None,
                    session_id: str | None = None, prefix: str = "", root: str | None = None,
                    chain_length: int = 0) -> dict:
    """worker-pool の内部の関数。pin_model はここだけ受ける（S-8.8、S-8.12 の 14）。"""
    req = dict(body)
    if not WORKDIR_RE.match(str(req.get("workdir_id") or "")):
        req["workdir_id"] = req.get("workdir_id")
    job = store.create(req, session_id=session_id or req.get("session_id") or str(uuid.uuid4()),
                       prefix=prefix, root=root, chain_length=chain_length)
    if pin_model:
        job["pin_model"] = pin_model
    return job


def submit(body: Any, store: JobsStore, *, role_queue_max: int = 5) -> dict:
    """Public submit, with an optional transactionally bound idempotency key."""
    req = validate_job_body(body)
    digest = hashlib.sha256(json.dumps(req, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    key = req.get("idempotency_key")
    with store._lock:
        store._conn.execute("BEGIN IMMEDIATE")
        try:
            row = store._conn.execute("SELECT id,request_digest FROM jobs WHERE idempotency_key=?", (key,)).fetchone() if key else None
            if row:
                if row["request_digest"] != digest:
                    raise IdempotencyConflict("idempotency_key_conflict")
                job = store.get(row["id"])
                result = {"job_id": job["id"], "state": "queued" if job["state"] == "reserving" else job["state"],
                          "role": job["role"], "workdir_id": job.get("workdir_id") or job["id"], "_replayed": True}
            else:
                prior_id = req.get("prior_job_id")
                if prior_id:
                    prior = store.prior_execution(prior_id)
                    if not prior:
                        raise ValidationError("prior_job_execution_unavailable")
                    req = {**req, "_excluded_family": prior["family"], "_excluded_model": prior["model"]}
                pending = store.list(state="queued", limit=1000) + store.list(state="reserving", limit=1000)
                queued = sum(1 for j in pending if j.get("role") == req["role"])
                if queued >= role_queue_max:
                    result = {"job_id": None, "state": "skipped", "reason": "queue_full", "role": req["role"]}
                else:
                    job = submit_internal(store, {**req, "_request_digest": digest}, session_id=str(uuid.uuid4()))
                    result = {"job_id": job["id"], "state": "queued", "role": req["role"],
                              "workdir_id": job.get("workdir_id") or job["id"]}
            store._conn.execute("COMMIT")
        except BaseException:
            store._conn.execute("ROLLBACK")
            raise
    return result


def job_public(job: dict) -> dict:
    """GET /v1/jobs/{id}（S-8.6）。"""
    return {
        "id": job["id"],
        "state": "queued" if job.get("state") == "reserving" else job.get("state"),
        "cancel_requested": bool(job.get("cancel_requested")),
        "reason": job.get("reason", ""),
        "model": job.get("model"),
        "family": job.get("family"),
        "tool_free": job.get("tool_free") == 1,
        "tool_free_evidence": job.get("tool_free_evidence") or None,
        "request_digest": job.get("request_digest"),
        "prior_job_id": (job.get("request") or {}).get("prior_job_id"),
        "verify_profile": (job.get("request") or {}).get("verify") if isinstance((job.get("request") or {}).get("verify"), str) else None,
        "role": job.get("role"),
        "workdir_id": job.get("workdir_id"),
        "session_id": job.get("session_id"),
        "root": job.get("root"),
        "chain_length": job.get("chain_length"),
        "verify": {"rc": job.get("verify_rc"), "log_tail": job.get("verify_log_tail", "")},
        "report": {"status": job.get("report_status"), "human_action": job.get("human_action", ""),
                   "text": job.get("report_text", "")},
        "changed_files": job.get("changed_files") or [],
        "started_at": job.get("started_at"),
        "ended_at": job.get("ended_at"),
    }


def build_routes(svc: PoolService, *, work_side: bool):
    from axcommon.http import read_json as _read_json

    async def post_jobs(request: Request):
        body, err = await _read_json(request)
        if err:
            return err
        try:
            res = submit(body, svc.store)
        except IdempotencyConflict as e:
            return _err(409, "idempotency_key_conflict", str(e))
        except ValidationError as e:
            code = "pin_model_not_allowed" if str(e) == "pin_model_not_allowed" else "bad_request"
            status = 400 if str(e) == "pin_model_not_allowed" else 422
            return _err(status, code, str(e))
        if res.get("reason") == "queue_full":
            return JSONResponse(res, status_code=200)
        replayed = res.pop("_replayed", False)
        job = svc.store.get(res["job_id"])
        if job and not replayed:
            svc.emit("job.state", job, {"job_id": job["id"], "state": job["state"]})
        return JSONResponse(res, status_code=202)

    async def get_job(request: Request):
        job = svc.store.get(request.path_params["job_id"])
        if job is None:
            return json_error(404, "not_found", "その ID のジョブがない")
        return JSONResponse(job_public(job))

    async def list_jobs(request: Request):
        q = request.query_params
        rows = svc.store.list(state=q.get("state"), role=q.get("role"))
        if q.get("state") == "queued":
            rows += svc.store.list(state="reserving", role=q.get("role"))
        return JSONResponse({"jobs": [job_public(r) for r in rows[: 200]]})

    async def cancel_job(request: Request):
        job = svc.store.cancel(request.path_params["job_id"])
        if job is None:
            return json_error(404, "not_found", "その ID のジョブがない")
        return JSONResponse({"ok": True, "job": job_public(job)})

    async def get_slots(request: Request):
        rows = svc.store.list(limit=1000)
        in_use = [j for j in rows if j.get("state") in ("launching", "submitted", "running")]
        return JSONResponse({"in_use": len(in_use), "jobs": [job_public(r) for r in in_use]})

    async def get_resources(request: Request):
        from starlette.concurrency import run_in_threadpool
        from axcommon.client import ApiError
        if svc.resources is None:
            return _err(503, "resources_unavailable")
        name = request.url.path.rsplit("/", 1)[-1]
        allowed = {"model", "role", "since"} if name == "ledger" else {"job_id"}
        if set(request.query_params) - allowed or any(len(request.query_params.getlist(k)) != 1 for k in request.query_params):
            return _err(400, "query_not_allowed")
        try:
            data = await run_in_threadpool(svc.resources.get, "/v1/" + name, params=dict(request.query_params))
            return JSONResponse(data)
        except ApiError as exc:
            return _err(exc.status, exc.code)
        except Exception:
            return _err(503, "resources_unavailable")

    async def get_status(request: Request):
        rows = svc.store.list(limit=1000)
        queued = sum(1 for r in rows if r.get("state") == "queued")
        with svc._settings_lock:
            settings = deepcopy(svc._settings)
        return JSONResponse({**settings, "running": [job_public(r) for r in rows if r.get("state") in ("launching", "submitted", "running")], "queued": queued})

    async def post_apply(request: Request):
        from axcommon.creds import read_secret_optional
        expected = read_secret_optional("control-apply-token")
        header = request.headers.get("authorization", "")
        if not expected or not hmac.compare_digest(header, "Bearer " + expected):
            return _err(403, "forbidden")
        body, err = await _read_json(request)
        if err:
            return err
        try:
            applied = svc.apply_effective(body)
        except ValueError:
            return _err(400, "bad_request")
        if not applied:
            return _err(409, "stale_revision")
        return JSONResponse({"ok": True})

    async def capabilities(request: Request):
        return JSONResponse({"change_safety": {"version": 1, "tool_free_required": True,
                                                "runner": "chat-json", "host_profile": "tool-free-chat-v1"}})

    async def recipe_result(request: Request):
        if getattr(request.state, "caller", None) != "change":
            return _err(404, "not_found")
        rid = request.path_params["request_id"]
        if not re.fullmatch(r"[a-z0-9-]{1,40}", rid):
            return _err(404, "not_found")
        with svc.store._lock:
            row = svc.store._conn.execute("SELECT * FROM verification_recipes WHERE id=?", (rid,)).fetchone()
        if not row or row["owner"] != request.state.caller:
            return _err(404, "not_found")
        if row["response"]:
            return JSONResponse(json.loads(row["response"]))
        scope = json.loads(row["request"])
        try:
            result = await asyncio.to_thread(svc.dispatcher.launcher.job_verify_recipe, rid, scope)
            from axcommon import schemas
            if (not isinstance(result, dict) or schemas.errors("ext/b6b/job-verify-recipe-result", result)
                    or result.get("prestart_isolation_enforced") is not True or result.get("request_id") != rid
                    or any(result.get(k) != v for k, v in scope.items())
                    or not isinstance(result.get("recipe_snapshot_id"), str)
                    or not re.fullmatch(r"vs-[0-9a-f]{32}", result["recipe_snapshot_id"])
                    or not isinstance(result.get("recipe_version"), str) or not 0 < len(result["recipe_version"]) <= 200
                    or not isinstance(result.get("allow_hosts"), list) or len(result["allow_hosts"]) > 64
                    or any(not isinstance(h, str) or not 0 < len(h) <= 253 for h in result["allow_hosts"])):
                return _err(503, "verification_recipe_unconfirmed")
            encoded = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            with svc.store._lock:
                svc.store._conn.execute("BEGIN IMMEDIATE")
                try:
                    current = svc.store._conn.execute("SELECT response FROM verification_recipes WHERE id=?", (rid,)).fetchone()
                    if current["response"] and current["response"] != encoded:
                        svc.store._conn.execute("ROLLBACK")
                        return _err(503, "verification_recipe_unconfirmed")
                    svc.store._conn.execute("UPDATE verification_recipes SET snapshot_id=?,response=? WHERE id=?", (result["recipe_snapshot_id"], encoded, rid))
                    svc.store._conn.execute("COMMIT")
                except BaseException:
                    svc.store._conn.execute("ROLLBACK")
                    raise
        except Exception:
            return _err(503, "verification_recipe_unconfirmed")
        return JSONResponse(result)

    async def recipe_prepare(request: Request):
        if getattr(request.state, "caller", None) != "change":
            return _err(404, "not_found")
        rid = request.path_params["request_id"]
        if not re.fullmatch(r"[a-z0-9-]{1,40}", rid):
            return _err(422, "bad_request")
        body, err = await _read_json(request)
        if err:
            return err
        if (not isinstance(body, dict) or set(body) != {"project", "target", "recipe_id"}
                or not isinstance(body["project"], str) or not 0 < len(body["project"]) <= 200
                or any(not isinstance(body[k], str) or not re.fullmatch(r"[a-z0-9-]{1,40}", body[k]) for k in ("target", "recipe_id"))):
            return _err(422, "bad_request")
        encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        with svc.store._lock:
            svc.store._conn.execute("BEGIN IMMEDIATE")
            try:
                row = svc.store._conn.execute("SELECT owner,request FROM verification_recipes WHERE id=?", (rid,)).fetchone()
                if row and row["owner"] != request.state.caller:
                    svc.store._conn.execute("ROLLBACK")
                    return _err(404, "not_found")
                if row and row["request"] != encoded:
                    svc.store._conn.execute("ROLLBACK")
                    return _err(409, "verification_scope_conflict")
                if not row:
                    svc.store._conn.execute("INSERT INTO verification_recipes(id,owner,request,created_at) VALUES (?,?,?,?)", (rid, request.state.caller, encoded, timeutil.now_ts()))
                svc.store._conn.execute("COMMIT")
            except BaseException:
                svc.store._conn.execute("ROLLBACK")
                raise
        return await recipe_result(request)

    async def verification_result(request: Request):
        if getattr(request.state, "caller", None) != "change":
            return _err(404, "not_found")
        jid = request.path_params["job_id"]
        if not re.fullmatch(r"[a-z0-9-]{1,40}", jid):
            return _err(404, "not_found")
        with svc.store._lock:
            row = svc.store._conn.execute("SELECT owner,request FROM verification_jobs WHERE id=?", (jid,)).fetchone()
        if not row or row["owner"] != request.state.caller:
            return _err(404, "not_found")
        scope = json.loads(row["request"])
        with svc.store._lock:
            recipe = svc.store._conn.execute("SELECT owner,request,response FROM verification_recipes WHERE snapshot_id=?", (scope.get("recipe_snapshot_id"),)).fetchone()
        if not recipe or recipe["owner"] != request.state.caller or not recipe["response"]:
            return _err(503, "verification_result_unconfirmed")
        snapshot = json.loads(recipe["response"])
        if any(scope.get(k) != snapshot[k] for k in ("project", "target", "recipe_id")):
            return _err(503, "verification_result_unconfirmed")
        try:
            result = await asyncio.to_thread(svc.dispatcher.launcher.job_verify_result, jid)
            from axcommon import schemas
            if (not isinstance(result, dict) or result.get("state") == "unknown" or result.get("job_id") != jid
                    or any(result.get(k) != v for k, v in scope.items())
                    or result.get("recipe_version") != snapshot["recipe_version"]
                    or result.get("allow_hosts") != (snapshot["allow_hosts"] if scope["stage"] == "dependency" else [])
                    or result.get("network_mode") != ("allowlist" if scope["stage"] == "dependency" else "none")
                    or schemas.errors("ext/b6b/job-verify-result", result)):
                return _err(503, "verification_result_unconfirmed")
        except Exception:
            return _err(503, "verification_result_unconfirmed")
        return JSONResponse(result)

    async def verification_prepare(request: Request):
        if getattr(request.state, "caller", None) != "change":
            return _err(404, "not_found")
        jid = request.path_params["job_id"]
        if not re.fullmatch(r"[a-z0-9-]{1,40}", jid):
            return _err(422, "bad_request")
        body, err = await _read_json(request)
        if err:
            return err
        from axcommon import schemas
        if not isinstance(body, dict) or schemas.errors("ext/b6b/job-verify-prepare", body):
            return _err(422, "bad_request")
        encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        with svc.store._lock:
            svc.store._conn.execute("BEGIN IMMEDIATE")
            try:
                snapshot = svc.store._conn.execute("SELECT owner,request,response FROM verification_recipes WHERE snapshot_id=?", (body["recipe_snapshot_id"],)).fetchone()
                if (not snapshot or snapshot["owner"] != request.state.caller or not snapshot["response"]
                        or any(json.loads(snapshot["request"]).get(k) != body[k] for k in ("project", "target", "recipe_id"))):
                    svc.store._conn.execute("ROLLBACK")
                    return _err(404, "not_found")
                row = svc.store._conn.execute("SELECT owner,request FROM verification_jobs WHERE id=?", (jid,)).fetchone()
                if row and row["owner"] != request.state.caller:
                    svc.store._conn.execute("ROLLBACK")
                    return _err(404, "not_found")
                if row and row["request"] != encoded:
                    svc.store._conn.execute("ROLLBACK")
                    return _err(409, "verification_scope_conflict")
                if not row:
                    if svc.store._conn.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
                        svc.store._conn.execute("ROLLBACK")
                        return _err(409, "verification_scope_conflict")
                    svc.store._conn.execute("INSERT INTO verification_jobs(id,owner,request,created_at) VALUES (?,?,?,?)",
                                            (jid, request.state.caller, encoded, timeutil.now_ts()))
                svc.store._conn.execute("COMMIT")
            except BaseException:
                svc.store._conn.execute("ROLLBACK")
                raise
        if not row:
            try:
                await asyncio.to_thread(svc.dispatcher.launcher.job_verify_prepare, jid, body)
            except Exception:
                return _err(503, "verification_result_unconfirmed")
        return await verification_result(request)

    if work_side:
        return [
            Route("/v1/capabilities", capabilities, methods=["GET"]),
            Route("/v1/verification/recipes/{request_id}", recipe_prepare, methods=["POST"]),
            Route("/v1/verification/recipes/{request_id}", recipe_result, methods=["GET"]),
            Route("/v1/verification/jobs/{job_id}", verification_prepare, methods=["POST"]),
            Route("/v1/verification/jobs/{job_id}", verification_result, methods=["GET"]),
            Route("/v1/jobs", post_jobs, methods=["POST"]),
            Route("/v1/jobs/{job_id}", get_job, methods=["GET"]),
            Route("/v1/jobs", list_jobs, methods=["GET"]),
            Route("/v1/jobs/{job_id}/cancel", cancel_job, methods=["POST"]),
            Route("/status", get_status, methods=["GET"]),
        ]
    return [
        Route("/v1/jobs", post_jobs, methods=["POST"]),
        Route("/v1/jobs/{job_id}", get_job, methods=["GET"]),
        Route("/v1/jobs", list_jobs, methods=["GET"]),
        Route("/v1/jobs/{job_id}/cancel", cancel_job, methods=["POST"]),
        Route("/v1/slots", get_slots, methods=["GET"]),
        Route("/v1/ledger", get_resources, methods=["GET"], name="ledger"),
        Route("/v1/selections", get_resources, methods=["GET"], name="selections"),
        Route("/status", get_status, methods=["GET"]),
        Route("/apply", post_apply, methods=["POST"]),
    ]
