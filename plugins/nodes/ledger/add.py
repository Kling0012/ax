"""ledger.add: 台帳に ToDo を作る（S-7.4）。LLM は使わない。同じ項目・同じノードでは 1 件だけ（ID が決まっているので、再実行しても増えない）。
サブループが作れるのは作成まで。遷移（transition）は呼べない（S-7.10、F-18.4）。"""
from __future__ import annotations

from axcommon.client import ApiError

from runtime.subloop.nodeapi import error, ok
from runtime.subloop.nodelib import api_error_out, short_hash

schema = {
    "type": "object",
    "properties": {"title": {"type": "string", "minLength": 1}, "state": {"enum": ["todo", "parked", "waiting_user"]}, "owner": {"enum": ["system", "user"]},
                   "reason": {"type": ["string", "null"]}, "check_at": {"type": ["string", "null"]}, "card": {"type": ["object", "null"]},
                   "depends_on": {"type": "array", "items": {"type": "string"}}, "artifacts": {"type": "array", "items": {"type": "string"}},
                   "resume_ref": {"type": ["object", "null"]}},
    "required": ["title"], "additionalProperties": False,
}
META = {"statuses": ["ok", "error"]}


async def run(ctx, inp):
    w = inp["with"]
    tid = "t-" + short_hash(ctx.svc.plugin_id, ctx.item_id, ctx.node_id, w["title"], n=20)
    todo = {k: v for k, v in w.items() if v is not None}
    todo.update(id=tid, state=w.get("state", "todo"), owner=w.get("owner", "system"), subloop=ctx.svc.plugin_id, project=(ctx.item or {}).get("project"),
                source={"run_id": ctx.run_id, "item_id": ctx.item_id})
    todo = {k: v for k, v in todo.items() if v is not None}
    try:
        try:
            got = await ctx.call(ctx.svc.ledger.get_todo, tid)
            if got:
                return ok({"todo_id": tid, "reused": True})
        except ApiError as e:
            if e.status != 404:
                raise
        await ctx.call(ctx.svc.ledger.create_todo, todo)
    except ApiError as e:
        return api_error_out(e)
    return ok({"todo_id": tid})
