"""env.cancel: 環境のジョブを 1 つ取り消す（S-7.4）。指定した job_id だけ（`POST /v1/env/jobs/{id}/cancel`）。LLM は使わない。取り消せなくても（終了済み）ok。"""
from __future__ import annotations

from axcommon.client import ApiError

from runtime.subloop.nodeapi import error, ok
from runtime.subloop.nodelib import api_error_out

schema = {"type": "object", "properties": {"job_id": {"type": "string", "pattern": "^[a-z0-9-]{1,60}$"}}, "required": ["job_id"], "additionalProperties": False}
META = {"statuses": ["ok", "error"]}


async def run(ctx, inp):
    jid = inp["with"]["job_id"]
    if ctx.svc.resources is None:
        return error("resources_unavailable", "resources への接続が無い")
    try:
        await ctx.call(ctx.svc.resources.post, f"/v1/env/jobs/{jid}/cancel", {})
    except ApiError as e:
        if e.status in (404, 409):
            return ok({"job_id": jid, "already_ended": True})
        return api_error_out(e)
    return ok({"job_id": jid})
