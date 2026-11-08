"""gh.label: Issue・PR にラベルを付ける。外へ影響する型（outputs: github.label）。付いていれば何もしない。"""
from __future__ import annotations

from axcommon.client import ApiError

from runtime.subloop.nodeapi import error, ok
from runtime.subloop.nodelib import api_error_out, short_hash

schema = {
    "type": "object",
    "properties": {"repo": {"type": "string", "pattern": r"^[\w.\-]+/[\w.\-]+$"}, "number": {"type": "integer", "minimum": 1},
                   "add": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1}},
    "required": ["repo", "number", "add"], "additionalProperties": False,
}
META = {"statuses": ["ok", "error"], "output": "github.label", "launching": True}


async def run(ctx, inp):
    w = inp["with"]
    if ctx.svc.github is None:
        return error("github_unavailable", "github-token が渡されていない（manifest の secrets）")
    key = "gh-label-" + short_hash(w["repo"], str(w["number"]), ",".join(sorted(w["add"])))
    try:
        have = set(await ctx.call(ctx.svc.github.labels, w["repo"], w["number"]))
        todo = [x for x in w["add"] if x not in have]
        if not todo:
            return ok({"added": [], "reused": True})
        await ctx.launching(key, {"repo": w["repo"], "number": w["number"], "add": w["add"]})
        await ctx.call(ctx.svc.github.add_labels, w["repo"], w["number"], todo)
    except ApiError as e:
        return api_error_out(e)
    return ok({"added": todo})


async def reconcile(ctx, key):
    ref = ctx.store.get_node_run(ctx.nr_id)["external_ref"]
    have = set(await ctx.call(ctx.svc.github.labels, ref["repo"], ref["number"]))
    if set(ref["add"]) <= have:
        return {"found": True, "out": ok({"added": ref["add"], "reconciled": True})}
    return {"found": False}
