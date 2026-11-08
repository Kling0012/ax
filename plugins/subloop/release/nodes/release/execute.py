"""Execute a persisted release batch through its publication service."""
from __future__ import annotations

from runtime.subloop.nodeapi import error, ok

schema = {
    "type": "object",
    "properties": {"batch_id": {"type": "string", "minLength": 1}},
    "required": ["batch_id"],
    "additionalProperties": False,
}
META = {"statuses": ["ok", "error"], "output": "env.run", "launching": True}


async def run(ctx, inp):
    service = getattr(ctx.svc, "release_service", None)
    if service is None:
        return error("release_service_unavailable", "Release service is not configured", expected=True)
    from plugins.subloop.release.gate.integration import GatePorts, PersistentReleaseGate
    ports = getattr(service, "ports", None)
    if not isinstance(ports, GatePorts) or not isinstance(getattr(ports.gate, "__self__", None), PersistentReleaseGate):
        return error("release_gate_unavailable", "Publication gate is not configured", expected=True)
    batch_id = inp["with"]["batch_id"]
    await ctx.launching("release:" + batch_id, {"batch_id": batch_id})
    try:
        result = await ctx.call(service.execute, batch_id, explicit={"dry_run": ctx.knobs.get("dry_run", True)})
    except Exception:
        return error("release_service_failed", "Release service failed", expected=True)
    if result.get("outcome") in ("success", "skipped"):
        return ok(result)
    return error("release_held", "Release did not complete", expected=True, data=result)


async def reconcile(ctx, key):
    service = getattr(ctx.svc, "release_service", None)
    if service is None:
        return None
    try:
        batch = await ctx.call(service.store.batch, key.removeprefix("release:"))
    except Exception:
        return None
    result = batch.get("last_result")
    if batch.get("state") == "success" and isinstance(result, dict):
        return {"found": True, "out": ok(result)}
    return None
