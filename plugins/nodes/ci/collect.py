"""ci.collect: 外部 CI の実行の結果を回収する。成功で ok、失敗で bug、まだ実行中なら再試行できる error（system.expected）。"""
from __future__ import annotations

from axcommon.client import ApiError

from runtime.subloop.nodeapi import bug, error, ok
from runtime.subloop.nodelib import api_error_out

schema = {
    "type": "object",
    "properties": {"repo": {"type": "string", "pattern": r"^[\w.\-]+/[\w.\-]+$"}, "marker": {"type": "string"}, "run_id": {"type": "integer"}, "request": {"type": "object"}},
    "required": ["repo"], "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "error"]}


async def run(ctx, inp):
    w = inp["with"]
    if "request" in w:
        service = getattr(ctx.svc, "public_ci", None)
        if service is None:
            return error("public_ci_unavailable", "public CI adapter is not configured", expected=True)
        request = w["request"]
        if request.get("public_repo") != w["repo"]:
            return error("ci_request_invalid", "public repository does not match", expected=True)
        result = await ctx.call(service.collect, request)
        if not result.get("collected"):
            return error("ci_uncollected", result.get("reason", "uncollected"), expected=True, data=result)
        if result.get("result") == "success":
            return ok(result)
        return bug("ci_failed", "public CI failed", data=result)
    if ctx.svc.github is None:
        return error("github_unavailable", "github-token が渡されていない（manifest の secrets）")
    try:
        rid = w.get("run_id")
        if rid is None:
            for r in await ctx.call(ctx.svc.github.list_runs, w["repo"], event="workflow_dispatch"):
                if w.get("marker") and (w["marker"] in (r.get("display_title") or "") or w["marker"] in (r.get("name") or "")):
                    rid = r.get("id")
                    break
        if rid is None:
            return error("ci_run_not_found", "印に合う実行が見つからない", expected=True)
        run_ = await ctx.call(ctx.svc.github.get_run, w["repo"], rid)
    except ApiError as e:
        return api_error_out(e)
    if run_.get("status") != "completed":
        return error("ci_running", "CI がまだ終わっていない", expected=True)
    if run_.get("conclusion") == "success":
        return ok({"run_id": rid, "conclusion": "success"})
    return bug("ci_failed", f"conclusion={run_.get('conclusion')}", data={"run_id": rid, "conclusion": run_.get("conclusion")})
