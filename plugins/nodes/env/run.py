"""env.run: 実行環境（S-25）で作業を走らせる（S-7.4、D-6.3）。環境の選択と情報の区分の確認は資源の口（resources）が行い、このノードは
`POST /v1/env/jobs` を出して終わりまで待ち、成果物の一覧を回収するだけ。環境の中で LLM を使う種別は、その系統の枠を使う。

- `with` は資源の口の依頼（S-25.3）: requires、sensitivity、entry、outputs、secrets、budget_min、stall_min、irreversible、scope、idempotent、idempotency_key ほか。
  job_id は決まっていなければ「項目・ノードの鍵」から作る（再実行しても同じ job_id。資源の口が同じ job_id を二重に受けない）。
- irreversible な依頼は launching を書き、reconcile は `GET /v1/env/jobs/{id}` で照合する（見つからなければ未実行）。
- 終わりの状態: succeeded で ok、failed は bug、timed_out・stalled・lost・cancelled・rejected は error。
"""
from __future__ import annotations

from axcommon.client import ApiError, Unavailable

from runtime.subloop.nodeapi import bug, error, ok
from runtime.subloop.nodelib import api_error_out, short_hash

schema = {
    "type": "object",
    "properties": {"job_id": {"type": "string", "pattern": "^[a-z0-9-]{1,60}$"}, "requires": {"type": "array", "items": {"type": "string"}},
                   "sensitivity": {"enum": ["public", "private", "sensitive"]}, "entry": {"type": "object"}, "outputs": {"type": "array", "items": {"type": "string"}},
                   "secrets": {"type": "array", "items": {"type": "string"}}, "budget_min": {"type": "integer", "minimum": 1}, "stall_min": {"type": "integer", "minimum": 1},
                   "irreversible": {"type": "boolean"}, "user_requested": {"type": "boolean"}, "scope": {"enum": ["one", "each"]}, "idempotent": {"type": "boolean"},
                   "idempotency_key": {"type": "string"}, "trial_for": {"type": "string"}},
    "required": ["requires", "sensitivity", "entry"], "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "error"], "launching": True}
TERMINAL = ("succeeded", "failed", "timed_out", "stalled", "cancelled", "lost", "rejected")


def _job_id(ctx, w) -> str:
    return w.get("job_id") or "env-" + short_hash(ctx.svc.plugin_id, ctx.item_id, ctx.node_id, str(ctx.attempt), n=24)


async def _wait(ctx, jid: str) -> dict:
    from axcommon import timeutil

    end = timeutil.parse_ts(ctx.deadline)
    last: dict = {"state": "queued"}
    while True:
        try:
            j = await ctx.call(ctx.svc.resources.get, f"/v1/env/jobs/{jid}")
            if isinstance(j, dict):
                last = j
                if j.get("state") in TERMINAL:
                    return j
        except (ApiError, Unavailable):
            pass
        if timeutil.now() >= end:
            return last
        await ctx.sleep(ctx.svc.poll_s)


async def run(ctx, inp):
    w = dict(inp["with"])
    if ctx.svc.resources is None:
        return error("resources_unavailable", "resources への接続が無い")
    jid = _job_id(ctx, w)
    body = {k: v for k, v in w.items() if k != "job_id"}
    body["job_id"] = jid
    body.setdefault("scope", "one")
    ref = {"job_id": jid}
    if w.get("irreversible"):
        await ctx.launching(f"env-run:{jid}", ref)
    else:
        ctx.set_ref(ref)
    try:
        await ctx.call(ctx.svc.resources.post, "/v1/env/jobs", body)
    except ApiError as e:
        if e.status != 409:  # 409 は同じ job_id を受付済み（再実行）
            return api_error_out(e)
    job = await _wait(ctx, jid)
    return await _finish(ctx, jid, job, w)


async def _finish(ctx, jid, job, w):
    st = job.get("state")
    data = {"job_id": jid, "env_id": job.get("env_id"), "state": st}
    if st == "succeeded":
        try:
            arts = await ctx.call(ctx.svc.resources.get, f"/v1/env/jobs/{jid}/artifacts")
        except (ApiError, Unavailable):
            arts = None
        data["artifacts"] = arts if isinstance(arts, (list, dict)) else []
        return ok(data)
    if st == "failed":
        return bug("env_job_failed", str(job.get("reason") or "failed"), data=data)
    return error(f"env_job_{st}", f"環境のジョブが {st}", expected=st in ("timed_out", "queued", "running"), data=data)


async def reconcile(ctx, key):
    jid = key.split(":", 1)[1]
    try:
        job = await ctx.call(ctx.svc.resources.get, f"/v1/env/jobs/{jid}")
    except ApiError as e:
        if e.status == 404:
            return {"found": False}
        return None
    except Unavailable:
        return None
    if not isinstance(job, dict):
        return None
    if job.get("state") in TERMINAL:
        return {"found": True, "out": await _finish(ctx, jid, job, {})}
    return {"found": True, "out": error("env_job_running", "再起動の後もまだ走っている。結果は GET /v1/env/jobs で回収する", expected=True, data={"job_id": jid})}
