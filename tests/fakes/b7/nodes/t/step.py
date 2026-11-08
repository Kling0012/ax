"""試験用: with.status を返す。with.fail_times 回までは with.fail_status、その後は with.status。"""
import asyncio

from runtime.subloop.nodeapi import bug, error, ok, skip
from tests.fakes.b7 import control

schema = {"type": "object", "properties": {"status": {"enum": ["ok", "bug", "error", "skip"]}, "fail_status": {"enum": ["bug", "error"]}, "fail_times": {"type": "integer"},
                                           "data": {"type": "object"}, "expected": {"type": "boolean"}, "boom": {"type": "boolean"}}, "additionalProperties": False}
META = {"statuses": ["ok", "bug", "error", "skip"]}


async def run(ctx, inp):
    w = inp["with"]
    k = (ctx.item_id, ctx.node_id)
    control.COUNTS[k] = control.COUNTS.get(k, 0) + 1
    control.LOG.append(k)
    await asyncio.sleep(0)
    if w.get("boom"):
        raise RuntimeError("boom")
    st = w.get("status", "ok")
    if control.COUNTS[k] <= w.get("fail_times", 0):
        st = w.get("fail_status", "bug")
    d = w.get("data", {})
    if st == "ok":
        return ok(d)
    if st == "skip":
        return skip(d)
    if st == "bug":
        return bug("t_bug", "bug", data=d)
    return error("t_error", "error", expected=w.get("expected", False), data=d)
