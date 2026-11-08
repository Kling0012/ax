"""gh.merge: PR をマージする。外へ影響する型（outputs: github.merge）。`sha`（head の完全 SHA）が一致するときだけ（head 一致のマージ。S-19）。
不一致は bug。二重実行の防止: 鍵は `<repo>#<番号>@<sha>`。reconcile は PR がマージ済みかを見る。"""
from __future__ import annotations

from axcommon.client import ApiError

from runtime.subloop.nodeapi import bug, error, ok
from runtime.subloop.nodelib import api_error_out

schema = {
    "type": "object",
    "properties": {"repo": {"type": "string", "pattern": r"^[\w.\-]+/[\w.\-]+$"}, "number": {"type": "integer", "minimum": 1},
                   "sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"}, "method": {"enum": ["merge", "squash", "rebase"]}},
    "required": ["repo", "number", "sha"], "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "error"], "output": "github.merge", "launching": True}


async def run(ctx, inp):
    w = inp["with"]
    if ctx.svc.github is None:
        return error("github_unavailable", "github-token が渡されていない（manifest の secrets）")
    key = f"gh-merge-{w['repo']}#{w['number']}@{w['sha']}"
    try:
        pr = await ctx.call(ctx.svc.github.get_pr, w["repo"], w["number"])
        if pr.get("merged"):
            return ok({"merged": True, "reused": True})
        if (pr.get("head") or {}).get("sha") != w["sha"]:
            return bug("head_mismatch", "PR の head が確かめた SHA と違う。マージしない")
        await ctx.launching(key, {"repo": w["repo"], "number": w["number"], "sha": w["sha"]})
        r = await ctx.call(ctx.svc.github.merge, w["repo"], w["number"], sha=w["sha"], method=w.get("method", "squash"))
    except ApiError as e:
        if e.status in (405, 409):
            return bug("not_mergeable", f"http {e.status}")
        return api_error_out(e)
    return ok({"merged": True, "sha": (r or {}).get("sha")})


async def reconcile(ctx, key):
    ref = ctx.store.get_node_run(ctx.nr_id)["external_ref"]
    pr = await ctx.call(ctx.svc.github.get_pr, ref["repo"], ref["number"])
    if pr.get("merged"):
        return {"found": True, "out": ok({"merged": True, "reconciled": True})}
    return {"found": False}
