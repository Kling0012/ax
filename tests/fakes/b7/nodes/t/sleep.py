"""試験用: with.seconds 待つ（ノードの timeout_s を超えさせる）。"""
import asyncio

from runtime.subloop.nodeapi import ok
from tests.fakes.b7 import control

schema = {"type": "object", "properties": {"seconds": {"type": "number"}, "first_only": {"type": "boolean"}}, "additionalProperties": False}
META = {"statuses": ["ok", "error"]}


async def run(ctx, inp):
    k = (ctx.item_id, ctx.node_id)
    control.COUNTS[k] = control.COUNTS.get(k, 0) + 1
    if not (inp["with"].get("first_only") and control.COUNTS[k] > 1):
        await asyncio.sleep(inp["with"].get("seconds", 0))
    return ok({"n": control.COUNTS[k], "continuation": ctx.continuation})
