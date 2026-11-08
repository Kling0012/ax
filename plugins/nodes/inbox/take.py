"""inbox.take: サブループの受け取り待ち（inbox）から項目を取り、item_source の出力にする（S-7.4、D-6.2）。

取る個数は max_parallel_items から、実行中と待っている項目の数を引いた数まで（`with.max` はその上限）。再起動しても queued は残る。
mode が run でなければ起動されない（needs_mode の既定 [run]）ので、draining・collect_only では取らない。
"""
from __future__ import annotations

from runtime.subloop.nodeapi import ok, skip

schema = {"type": "object", "properties": {"max": {"type": "integer", "minimum": 1}}, "additionalProperties": False}
META = {"statuses": ["ok", "skip"]}


async def run(ctx, inp):
    free = ctx.free_slots()
    cap = inp["with"].get("max")
    n = free if cap is None else min(free, int(cap))
    taken = ctx.store.inbox_take(n)
    if not taken:
        return skip({"items": []})
    items = []
    for it in taken:
        d = dict(it)
        d["item_id"] = it["item_key"]  # item_key は冪等キーで、項目の item_id になる（S-7.10）
        items.append(d)
    return ok({"items": items})
