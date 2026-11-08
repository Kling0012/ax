"""Work requests. Public payload validation stays owned by schemas/dest/."""
from __future__ import annotations

from axcommon import ids, schemas
from core.intake.dest.b12 import common, mappers


def register(app):
    app.state.intake.dest_handlers["work.request"] = handle


def handle(app, dest, body, ident):
    payload = body["payload"]
    errors = schemas.errors("dest/work.request.json", payload)
    if errors:
        return 422, {"error": {"code": "payload_schema", "message": "; ".join(errors[:3])}}
    role = payload.get("role", "impl")
    if role in {"impl", "impl_hard"}:
        return 200, submit_implementation(app, body, ident=ident)
    if role not in common.READ_ROLES:
        raise common.fault(422, "role_cannot_write")
    return 200, submit_read(app, body, ident=ident)


def submit_implementation(app, body, *, ident=None):
    """Persist request-to-ToDo identity before submitting the mapped item."""
    payload = body["payload"]
    key = body.get("idempotency_key") or body.get("id")
    if not isinstance(key, str) or not key:
        raise common.fault(422, "idempotency_key_required")
    e = common.engine(app)
    with e.lock:
        request_key = "work:" + key
        rec = e.db.get(request_key)
        if rec and (rec["payload"] != payload or (ident is not None and rec.get("owner") != ident.principal)):
            raise common.fault(409, "idempotency_mismatch")
        if rec is None:
            rec = {"payload": payload, "todo_id": ids.new_ulid()}
            if ident is not None:
                rec["owner"] = ident.principal
            e.db.put(request_key, rec)
        item = mappers.work_item(payload, rec["todo_id"])
    return common.submit_item(app, item)


def submit_read(app, body, *, ident=None):
    """Submit a read request with registered, fixed input references."""
    payload = body["payload"]
    schemas.validate("dest/work.request.json", payload)
    role = payload.get("role")
    if role not in common.READ_ROLES:
        raise common.fault(422, "role_cannot_write")
    e = common.engine(app)
    key = body.get("idempotency_key") or body.get("id")
    if not isinstance(key, str) or not key:
        raise common.fault(422, "idempotency_key_required")
    with e.lock:
        request_key = "read:" + key
        rec = e.db.get(request_key)
        if rec and (rec["payload"] != payload or (ident is not None and rec.get("owner") != ident.principal)):
            raise common.fault(409, "idempotency_mismatch")
        if rec and rec.get("job_id"):
            return {"todo_id": rec["todo_id"], "job_id": rec["job_id"], "duplicate": True}
        if rec and rec.get("sending"):
            # worker-pool has no idempotency-key field. A lost response must not
            # create another job; retain the ToDo for reconciliation.
            raise common.fault(409, "job_delivery_uncertain")
        if rec is None:
            rec = {"payload": payload, "todo_id": ids.new_ulid()}
            if ident is not None:
                rec["owner"] = ident.principal
            e.db.put(request_key, rec)
        item = {"item_key": rec["todo_id"], "todo_id": rec["todo_id"], **payload}
        e.ensure_todo(item)
        if not rec.get("initial_seq"):
            todo, history = e.read_history(rec["todo_id"])
            if (todo.get("state") != "todo" or len(history) != 1
                    or history[0].get("kind") != "created" or history[0].get("actor") != "intake"):
                raise common.fault(409, "todo_already_changed")
            rec["initial_seq"] = history[0]["seq"]
            e.db.put(request_key, rec)
        prompt = (payload["title"] + "\n\n" + payload["body"] + "\n\nFixed revision: " + payload["revision"]
                  + "\n\nRead /target. Write /work/research-report.json as an object with "
                    "type ax.research-report.v1, revision, and findings. Each finding must have "
                    "a statement and nonempty citations with path, line_start, line_end. "
                    "Cite registered materials under .ax-attachments/<id>/. "
                    "End the response with STATUS: ready only when the report is ready.")
        job = {"role": role, "prompt": prompt, "sensitivity": "private",
               "project": payload["project"], "todo_id": rec["todo_id"],
               "read_input": {"target": payload["target"], "revision": payload["revision"]},
               "attachments": payload.get("attachments", []), "verify": "ax:research-report-v1"}
        schemas.validate("job-request", job)
        rec["sending"] = True
        e.db.put(request_key, rec)
        result = e.pool.submit(job)
        rec["job_id"] = result.get("job_id")
        if not rec["job_id"]:
            rec["sending"] = False
            e.db.put(request_key, rec)
            from datetime import timedelta
            from axcommon import timeutil
            try:
                e.ledger.transition(rec["todo_id"], "parked", result.get("reason", "queue_full"),
                                    expected_history_seq=rec["initial_seq"],
                                    check_at=timeutil.format_ts(timeutil.now() + timedelta(hours=1)))
            except common.ApiError as exc:
                if exc.status != 409:
                    raise
                return {"todo_id": rec["todo_id"], "state": "unchanged", "reason": "todo_changed"}
            return {"todo_id": rec["todo_id"], "state": "parked", "reason": result.get("reason", "queue_full")}
        e.db.put(request_key, rec)
        e.start_read(request_key, rec)
        return {"todo_id": rec["todo_id"], "job_id": rec["job_id"]}
