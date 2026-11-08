"""Authenticated work stop requests; active jobs are only requested to stop."""
from urllib.parse import quote

from axcommon import schemas
from axcommon.client import ApiClient, ApiError, Unavailable, service_url
from core.intake.dest.b10a.handler import _token
from core.intake.dest.b12 import common
from core.intake.vias import tier_sufficient

_TERMINAL_TODOS = frozenset({"done", "dropped"})
_TERMINAL_JOBS = frozenset({"ok", "failed", "timed_out", "cancelled", "lost", "rejected"})


def _error(status, code):
    return status, {"error": {"code": code, "message": code}}


def _handle(app, body, ident, operation):
    payload = body.get("payload")
    if not isinstance(payload, dict) or schemas.errors("dest/" + operation + ".json", payload):
        return _error(422, "payload_schema")
    if (ident is None or not getattr(ident, "principal", "")
            or not tier_sufficient(getattr(ident, "max_tier", ""), "operate")
            or getattr(ident, "via", "") not in {"local", "ssh", "tailnet", "hq"}
            or getattr(ident, "from_", "") != ("hq" if ident.via == "hq" else "commander")
            or (ident.via == "hq" and ident.principal != "user")):
        return _error(403, "forbidden")
    engine = common.engine(app)
    with engine.lock:
        records = [r for r in engine.db.work_requests()
                   if ("todo_id" in payload and r.get("todo_id") == payload["todo_id"])
                   or ("job_id" in payload and r.get("job_id") == payload["job_id"])]
        if not records:
            return _error(404, "not_found")
        if len(records) != 1:
            return _error(409, "ambiguous_target")
        record = records[0]
        if not record.get("owner") or record["owner"] != ident.principal:
            return _error(403, "not_owner")
        todo_id, job_id = record["todo_id"], record.get("job_id")
        ledger = ApiClient(service_url("ledger"), token=_token(), transport=app.state.intake.transport,
                           extra_headers={"X-AX-Via": ident.via})
        todo_path = "/v1/todos/" + quote(todo_id, safe="")
        try:
            todo = ledger.get(todo_path)
            todo = todo.get("data", todo)
            if todo.get("state") in _TERMINAL_TODOS:
                return 200, {"todo_id": todo_id, "state": todo["state"]}
            if (operation == "work.cancel" and todo.get("state") == "waiting_user"
                    and todo.get("user_only") and ident.via != "hq"):
                return _error(403, "user_only_card")
            if not job_id:
                return _error(409, "job_mapping_unavailable")
            job = engine.pool.get("/v1/jobs/" + quote(job_id, safe=""))
            if job.get("state") in _TERMINAL_JOBS:
                return 200, {"todo_id": todo_id, "job_id": job_id, "state": job["state"]}
            details = {"operation": operation, "principal": ident.principal, "job_id": job_id}
            if "reason" in payload:
                details["reason"] = payload["reason"]
            ledger.post(todo_path + "/work-requests", details)
            result = engine.pool.post("/v1/jobs/" + quote(job_id, safe="") + "/cancel", {})
            state = result.get("job", {}).get("state")
            if state in _TERMINAL_JOBS:
                return 200, {"todo_id": todo_id, "job_id": job_id, "state": state}
            return 202, {"todo_id": todo_id, "job_id": job_id, "state": state, "status": "stop_requested"}
        except ApiError as exc:
            return exc.status, exc.body or {"error": {"code": exc.code, "message": exc.message}}
        except Unavailable:
            return _error(503, "unavailable")


def cancel(app, dest, body, ident):
    return _handle(app, body, ident, "work.cancel")


def stop(app, dest, body, ident):
    return _handle(app, body, ident, "work.stop")


def register(app):
    app.state.intake.dest_handlers.update({"work.cancel": cancel, "work.stop": stop})
