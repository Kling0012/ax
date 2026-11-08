"""B7 discovers this node type from the plugin's nodes directory."""
from runtime.subloop.nodeapi import error

TYPE = "x.posts"
schema = {"type": "object", "properties": {
    "action": {"enum": ["collect", "draft", "send"]},
    "phase": {"enum": ["queue", "draft"]}}, "required": ["action"], "additionalProperties": False}
META = {"statuses": ["ok", "error", "skip"], "launching": True}


async def run(ctx, inp):
    binding = getattr(ctx.svc, "x_posts_binding", None)
    if binding is None:
        return error("x_posts_binding_missing", "X の環境・ワーカー・検査の接続が必要")
    if ctx.knobs.get("enabled", True) is not True:
        from runtime.subloop.nodeapi import ok
        return ok({"items": [], "reason": "disabled"})
    action = inp["with"]["action"]
    if action == "collect":
        return await binding.collect(ctx, phase=inp["with"].get("phase", "queue"))
    if action == "draft":
        return await binding.draft(ctx)
    return await binding.send(ctx)


async def reconcile(ctx, key):
    binding = getattr(ctx.svc, "x_posts_binding", None)
    return await binding.reconcile(ctx, key) if binding is not None else None
