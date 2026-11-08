"""gh.comment: Issue・PR にコメントを書く。外へ影響する型（outputs: github.comment）。公開の前に gate.secret_scan が要る（S-7.7 の 8）。

二重実行の防止: 本文の末尾に隠しの印 `<!-- ax:<鍵> -->` を付け、外部の操作の直前に launching を書く。再起動後の reconcile は、
コメントの一覧から印を探す（見つかれば書き終えている。再送しない）。
"""
from __future__ import annotations

from axcommon.client import ApiError

from runtime.subloop.nodeapi import error, ok
from runtime.subloop.nodelib import api_error_out, short_hash

schema = {
    "type": "object",
    "properties": {"repo": {"type": "string", "pattern": r"^[\w.\-]+/[\w.\-]+$"}, "number": {"type": "integer", "minimum": 1}, "body": {"type": "string", "minLength": 1}},
    "required": ["repo", "number", "body"], "additionalProperties": False,
}
META = {"statuses": ["ok", "error"], "output": "github.comment", "publishes": True, "launching": True}


def _key(ctx, w) -> str:
    return "gh-comment-" + short_hash(ctx.svc.plugin_id, ctx.item_id, ctx.node_id, w["repo"], str(w["number"]), w["body"])


def _marker(key: str) -> str:
    return f"<!-- ax:{key} -->"


async def run(ctx, inp):
    w = inp["with"]
    if ctx.svc.github is None:
        return error("github_unavailable", "github-token が渡されていない（manifest の secrets）")
    key = _key(ctx, w)
    try:
        existing = await ctx.call(ctx.svc.github.list_comments, w["repo"], w["number"])
        for c in existing:
            if _marker(key) in (c.get("body") or ""):
                return ok({"comment_id": c.get("id"), "reused": True})
        await ctx.launching(key, {"repo": w["repo"], "number": w["number"]})
        c = await ctx.call(ctx.svc.github.comment, w["repo"], w["number"], w["body"] + "\n\n" + _marker(key))
    except ApiError as e:
        return api_error_out(e)
    return ok({"comment_id": (c or {}).get("id")})


async def reconcile(ctx, key):
    ref = ctx.store.get_node_run(ctx.nr_id)["external_ref"]
    for c in await ctx.call(ctx.svc.github.list_comments, ref["repo"], ref["number"]):
        if _marker(key) in (c.get("body") or ""):
            return {"found": True, "out": ok({"comment_id": c.get("id"), "reconciled": True})}
    return {"found": False}
