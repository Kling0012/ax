"""notify: 通知を出す（S-7.4）。LLM は使わない。時系列記録に `change.notification`（state: requested）を書く。配送は通知の配送の部品が事象を読んで行う（S-19、S-22）。
同じ項目・同じノード・同じ内容では notification_id が同じ（再実行しても同じ ID。配送側が重複を除く）。本文は事象に入れない（題と短い文だけ。秘密を入れない）。"""
from __future__ import annotations

from runtime.subloop.nodeapi import ok
from runtime.subloop.nodelib import short_hash

TYPE = "notify"
schema = {
    "type": "object",
    "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 200}, "text": {"type": "string", "maxLength": 1000},
                   "level": {"enum": ["info", "warn", "alert"]}},
    "required": ["title"], "additionalProperties": False,
}
META = {"statuses": ["ok"]}


async def run(ctx, inp):
    w = inp["with"]
    nid = "n-" + short_hash(ctx.svc.plugin_id, ctx.item_id, ctx.node_id, w["title"], w.get("text", ""), n=20)
    ctx.emit("change.notification", nid, {"notification_id": nid, "item_id": ctx.item_id, "state": "requested", "attempt": 0, "next_retry_at": None,
                                          "title": w["title"], "text": w.get("text", ""), "level": w.get("level", "info")})
    return ok({"notification_id": nid})
