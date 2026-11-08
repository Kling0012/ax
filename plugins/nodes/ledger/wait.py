"""ledger.wait: ToDo の状態を期限付きで待つ（S-7.4）。until の状態のどれかになれば ok（data.outcome に状態名）。wait_s までに届かなければ
data.outcome=timeout の ok（`when: outcome:timeout` の辺で受けられる）。LLM は使わない。"""
from __future__ import annotations

from axcommon import timeutil
from axcommon.client import ApiError, Unavailable

from runtime.subloop.nodeapi import error, ok

schema = {
    "type": "object",
    "properties": {"todo_id": {"type": "string", "minLength": 1}, "until": {"type": "array", "items": {"enum": ["todo", "running", "review", "waiting_user", "parked", "done", "dropped"]}, "minItems": 1},
                   "wait_s": {"type": "integer", "minimum": 0}, "poll_s": {"type": "number", "minimum": 0}},
    "required": ["todo_id", "until"], "additionalProperties": False,
}
META = {"statuses": ["ok", "error"], "outcomes": ["timeout"]}


async def run(ctx, inp):
    w = inp["with"]
    end = timeutil.now().timestamp() + float(w.get("wait_s", 0))
    poll = float(w.get("poll_s", ctx.svc.poll_s))
    while True:
        try:
            t = await ctx.call(ctx.svc.ledger.get_todo, w["todo_id"])
            if isinstance(t, dict) and t.get("state") in w["until"]:
                return ok({"state": t["state"], "outcome": t["state"]})
        except ApiError as e:
            if e.status == 404:
                return error("todo_not_found", w["todo_id"])
        except Unavailable:
            pass
        if timeutil.now().timestamp() >= end:
            return ok({"outcome": "timeout"})
        await ctx.sleep(poll)
