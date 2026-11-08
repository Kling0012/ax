"""Bind the graph to the publisher engine without a production sender."""
from dataclasses import replace

from axcommon import timeutil
from runtime.subloop.nodeapi import eject, ok, skip


class StartedControl:
    """Restrict each action to the latest user's active start instruction."""
    def __init__(self, ex, delegate):
        self.ex, self.delegate = ex, delegate

    def effective(self, plugin_id):
        if self.ex.mode != "run":
            return {"mode": self.ex.mode}
        return self.delegate.effective(plugin_id)

    def check(self, target, operation):
        try:
            if self.ex.mode != "run" or self.delegate.check(target, operation) is not True:
                return False
            client = self.ex.svc.control
            commands = client.get("/v1/commands", params={"state": "active", "issued_by": "user"})["commands"]
            command = next((c for c in reversed(commands) if c.get("target") in (target["plugin_id"], "all")
                            and c.get("issued_by") == "user" and c.get("state") == "active"
                            and c.get("op") == "set_mode"), None)
            if not command or command.get("args", {}).get("mode") != "run":
                return False
            ids = command.get("args", {}).get("permission_ids")
            if not isinstance(ids, list) or not ids or not command.get("corr"):
                return False
            permissions = client.get("/v1/permissions")["permissions"]
            return any(p.get("id") in ids and p.get("enabled") is True
                       and p.get("source_instruction") == command["corr"] and not p.get("conditions")
                       and all(p.get(k) == target[k] for k in ("project", "repo", "destination"))
                       and operation in p.get("operations", []) for p in permissions)
        except Exception:
            return False


def bind_runtime(ex=None, *, engine=None, sender=None):
    if ex is None:
        raise NotImplementedError("an executor is required")
    if engine is not None:
        engine.sender = sender
        engine.control = StartedControl(ex, engine.control)

    async def preflight(ctx, inp):
        if ctx.ex.mode == "collect_only":
            if engine is not None:
                for target in engine.registry.for_plugin(ctx.svc.plugin_id):
                    await ctx.call(engine.run, target, scheduled_at=timeutil.now_ts())
            return skip()
        if engine is None or sender is None:
            if not ctx.store.get_meta("scheduled_publisher_unbound_todo"):
                try:
                    await ctx.call(ctx.svc.ledger.create_todo, {
                        "title": "Bind scheduled publisher sender", "state": "parked",
                        "owner": "system", "reason": "sender_unbound", "subloop": ctx.svc.plugin_id,
                        "check_at": timeutil.add_seconds(timeutil.now_ts(), 86400),
                        "source": {"run_id": ctx.run_id, "item_id": ctx.item_id}})
                    ctx.store.set_meta("scheduled_publisher_unbound_todo", "created")
                except Exception:
                    return eject("sender_unbound")
            return skip()
        targets = engine.registry.for_plugin(ctx.svc.plugin_id)
        if not any(engine.control.check(t, op) for t in targets for op in t.get("operations", [])):
            return eject("permission_denied")
        return ok()

    async def record(ctx, inp):
        return ok()

    async def outbox(ctx, inp):
        return ok()

    env_original = ex.registry.types["env.run"]
    worker_original = ex.registry.types["worker.run"]

    async def environment(ctx, inp):
        if ctx.node_id == "collect":
            return ok()
        if ctx.node_id != "send" or engine is None or sender is None:
            return eject("sender_unbound")
        try:
            targets = engine.registry.for_plugin(ctx.svc.plugin_id)
            if not targets:
                return skip()
            run = ctx.store.get_run(ctx.run_id) or {}
            when = run.get("planned_at") or timeutil.now_ts()
            outcomes = []
            for target in targets:
                outcomes.append(await ctx.call(engine.run, target, scheduled_at=when))
            if any(row.get("result") in ("failed", "parked", "denied") for row in outcomes):
                return eject("publisher_parked", data={"results": outcomes})
            return ok({"results": outcomes})
        except Exception:
            return eject("publisher_unavailable")

    async def worker(ctx, inp):
        if ctx.node_id == "decide":
            return ok()
        return await worker_original.run(ctx, inp)

    for name, fn in (("eigen.preflight", preflight), ("eigen.outbox_check", outbox), ("eigen.record", record)):
        ex.registry.types[name] = replace(ex.registry.types[name], run=fn)
    ex.registry.types["env.run"] = replace(env_original, run=environment)
    ex.registry.types["worker.run"] = replace(worker_original, run=worker)
    return ex
