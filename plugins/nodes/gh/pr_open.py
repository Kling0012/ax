"""gh.pr_open: PR を開く。外へ影響する型（outputs: github.pr）。公開の前に gate.secret_scan が要る。
二重実行の防止: 鍵は head のブランチ名。reconcile は head で既存の PR を探す。"""
from __future__ import annotations

from axcommon.client import ApiError

from runtime.subloop.nodeapi import error, ok
from runtime.subloop.nodelib import api_error_out

schema = {
    "type": "object",
    "properties": {"repo": {"type": "string", "pattern": r"^[\w.\-]+/[\w.\-]+$"}, "head": {"type": "string", "minLength": 1}, "base": {"type": "string", "minLength": 1},
                   "title": {"type": "string", "minLength": 1}, "body": {"type": "string"}},
    "required": ["repo", "head", "base", "title"], "additionalProperties": False,
}
META = {"statuses": ["ok", "error"], "output": "github.pr", "publishes": True, "launching": True}


async def run(ctx, inp):
    w = inp["with"]
    if ctx.svc.github is None:
        return error("github_unavailable", "github-token が渡されていない（manifest の secrets）")
    key = f"gh-pr-{w['repo']}:{w['head']}"
    try:
        pr = await ctx.call(ctx.svc.github.find_pr, w["repo"], w["head"])
        if pr:
            return ok({"number": pr["number"], "url": pr.get("html_url", ""), "reused": True})
        await ctx.launching(key, {"repo": w["repo"], "head": w["head"]})
        pr = await ctx.call(ctx.svc.github.pr_open, w["repo"], head=w["head"], base=w["base"], title=w["title"], body=w.get("body", ""))
    except ApiError as e:
        return api_error_out(e)
    return ok({"number": pr["number"], "url": pr.get("html_url", "")})


async def reconcile(ctx, key):
    ref = ctx.store.get_node_run(ctx.nr_id)["external_ref"]
    pr = await ctx.call(ctx.svc.github.find_pr, ref["repo"], ref["head"])
    if pr:
        return {"found": True, "out": ok({"number": pr["number"], "url": pr.get("html_url", ""), "reconciled": True})}
    return {"found": False}
