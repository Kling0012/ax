"""gh.list_issues: 条件に合う Issue を項目にする（LLM は使わない。S-7.4）。項目の ID は `<repo>#<番号>`。"""
from __future__ import annotations

from axcommon.client import ApiError

from runtime.subloop.nodeapi import error, ok
from runtime.subloop.nodelib import api_error_out

schema = {
    "type": "object",
    "properties": {"repo": {"type": "string", "pattern": r"^[\w.\-]+/[\w.\-]+$"}, "label": {"type": "string"},
                   "state": {"enum": ["open", "closed", "all"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
    "required": ["repo"], "additionalProperties": False,
}
META = {"statuses": ["ok", "error"]}


async def run(ctx, inp):
    w = inp["with"]
    if ctx.svc.github is None:
        return error("github_unavailable", "github-token が渡されていない（manifest の secrets）")
    try:
        rows = await ctx.call(ctx.svc.github.list_issues, w["repo"], label=w.get("label"), state=w.get("state", "open"), limit=w.get("limit", 50))
    except ApiError as e:
        return api_error_out(e)
    items = [{"item_id": f"{w['repo']}#{r['number']}", "repo": w["repo"], "number": r["number"], "title": r.get("title", ""),
              "body": r.get("body") or "", "url": r.get("html_url", ""), "labels": [x.get("name") for x in r.get("labels", []) if isinstance(x, dict)]}
             for r in rows]
    return ok({"items": items})
