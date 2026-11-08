"""ci.trigger: 外部 CI（GitHub Actions の workflow_dispatch）を起動する。外へ影響する型（outputs: ci.trigger）。
入力 `ax_marker` に鍵を入れて起動し、reconcile は同じ印の実行を探す（二重に起動しない）。"""
from __future__ import annotations

from axcommon.client import ApiError

from runtime.subloop.nodeapi import error, ok
from runtime.subloop.nodelib import api_error_out, short_hash

schema = {
    "type": "object",
    "properties": {"repo": {"type": "string", "pattern": r"^[\w.\-]+/[\w.\-]+$"}, "workflow": {"type": "string", "minLength": 1},
                   "ref": {"type": "string", "minLength": 1}, "inputs": {"type": "object"},
                   "private_repo": {"type": "string"}, "private_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                   "mode": {"enum": ["quick", "full"]}},
    "required": ["repo", "workflow", "ref"], "additionalProperties": False,
}
META = {"statuses": ["ok", "error"], "output": "ci.trigger", "launching": True}


async def run(ctx, inp):
    w = inp["with"]
    if "private_repo" in w or "private_sha" in w or "mode" in w:
        if not all(k in w for k in ("private_repo", "private_sha", "mode")):
            return error("ci_request_invalid", "private_repo, private_sha and mode are required", expected=True)
        service = getattr(ctx.svc, "public_ci", None)
        if service is None:
            return error("public_ci_unavailable", "public CI adapter is not configured", expected=True)
        key = "ci-" + short_hash(ctx.svc.plugin_id, ctx.item_id, ctx.node_id, w["repo"], w["private_repo"], w["private_sha"], w["mode"])
        await ctx.launching(key, {"public_ci": True, "repo": w["repo"], "private_repo": w["private_repo"],
                                  "private_sha": w["private_sha"], "mode": w["mode"]})
        try:
            request = await ctx.call(service.trigger, w["private_repo"], w["repo"], w["private_sha"],
                                     w["mode"], ref=w["ref"], workflow=w["workflow"])
        except Exception:
            return error("ci_dispatch_failed", "public CI dispatch failed", expected=True)
        return ok(request)
    if ctx.svc.github is None:
        return error("github_unavailable", "github-token が渡されていない（manifest の secrets）")
    key = "ci-" + short_hash(ctx.svc.plugin_id, ctx.item_id, ctx.node_id, w["repo"], w["workflow"], w["ref"])
    await ctx.launching(key, {"repo": w["repo"], "workflow": w["workflow"]})
    try:
        await ctx.call(ctx.svc.github.dispatch_workflow, w["repo"], w["workflow"], w["ref"], {**(w.get("inputs") or {}), "ax_marker": key})
    except ApiError as e:
        return api_error_out(e)
    return ok({"marker": key})


async def reconcile(ctx, key):
    ref = ctx.store.get_node_run(ctx.nr_id)["external_ref"]
    if ref.get("public_ci"):
        service = getattr(ctx.svc, "public_ci", None)
        if service is None:
            return {"found": False}
        store_key = f"dispatch:{ref['repo']}:{ref['private_repo']}:{ref['private_sha']}:{ref['mode']}"
        request = service.store.get(store_key)
        if not request:
            return {"found": False}
        request = await ctx.call(service.identify, request, store_key)
        return {"found": bool(request.get("run_id")), "out": ok(request)}
    runs = await ctx.call(ctx.svc.github.list_runs, ref["repo"], event="workflow_dispatch")
    for r in runs:
        if key in (r.get("display_title") or "") or key in (r.get("name") or ""):
            return {"found": True, "out": ok({"marker": key, "run_id": r.get("id"), "reconciled": True})}
    return {"found": False}
