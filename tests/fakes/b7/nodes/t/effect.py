"""試験用: 外へ影響する操作（二重実行の防止の試験）。launching を書き、EXTERNAL に鍵を足す。reconcile は EXTERNAL を見る。"""
import asyncio

from runtime.subloop.nodeapi import ok
from tests.fakes.b7 import control

schema = {"type": "object", "properties": {"key": {"type": "string"}, "reconcilable": {"type": "boolean"}}, "required": ["key"], "additionalProperties": False}
META = {"statuses": ["ok", "error"], "output": "t.effect", "launching": True}


async def run(ctx, inp):
    key = inp["with"]["key"]
    await ctx.launching(key, {"reconcilable": inp["with"].get("reconcilable", True)})
    if control.CRASH_BEFORE_OP["on"]:
        raise asyncio.CancelledError()
    control.EXTERNAL.append(key)
    if control.CRASH_AFTER_OP["on"]:
        raise asyncio.CancelledError()
    return ok({"key": key})


async def reconcile(ctx, key):
    ref = ctx.store.get_node_run(ctx.nr_id)["external_ref"] or {}
    if not ref.get("reconcilable", True):
        return None
    if key in control.EXTERNAL:
        return {"found": True, "out": ok({"key": key, "reconciled": True})}
    return {"found": False}
