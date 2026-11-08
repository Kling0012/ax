"""試験用: with.items をそのまま項目の一覧にする。"""
from runtime.subloop.nodeapi import ok

schema = {"type": "object", "properties": {"items": {"type": "array"}}, "additionalProperties": False}
META = {"statuses": ["ok", "error"]}


async def run(ctx, inp):
    return ok({"items": inp["with"].get("items", [])})
