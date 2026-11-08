"""Exact B12-1 adapters; no inferred wire contract or live backend activation."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .policy import within_declaration
from .registration import attribution, read_candidate, stage_registration


def registration_paths(ident: str) -> list[str]:
    return [f"plugins/subloop/{ident}", f"plugins/registry.d/sl-{ident}.yaml",
            "plugins/registry.yaml", f"compose.d/sl-{ident}.yaml"]


class CommonBridge:
    """Use with Service(stage=bridge.plan_registration).

    branch_root must expose a separate, read-only candidate view. Registration
    writes occur inside common's intake-owned snapshot, before its CI/ff/tag/
    deploy sequence. A trusted timeline subscriber must call Service.on_item_state;
    this adapter does not create a listener or change other finalizers.
    """
    def __init__(self, app, *, validate, template_root: Path, common=None):
        if common is None:
            from core.intake.dest.b12 import common
        self.app, self.common = app, common
        self.validate, self.template_root = validate, Path(template_root)

    @staticmethod
    def plan_registration(root, ident, manifest, *, template_root):
        # Staging is deferred until common has selected its immutable snapshot.
        return registration_paths(ident)

    def submit_item(self, item):
        service = getattr(getattr(self.app, "state", None), "b12_subloop", None)
        record = service.store.for_todo(item["todo_id"]) if service is not None else None
        result = self.common.submit_item(self.app, item,
                                         approval_id=record.get("approval_id") if record else None,
                                         metadata=attribution(record) if record else {})
        return {**result, "status": 429 if result.get("reason") == "inbox_full" else 202}

    def accept_branch(self, registration):
        engine = self.common.engine(self.app)
        key = registration["todo_id"]
        record = engine.record(key)
        if record["item"]["kind"] != "subloop_create" or record["item"]["branch"] != registration["branch"]:
            raise ValueError("common item does not match completed subloop item")
        # This method is a trusted Service port reached only after item.state.
        record["completed"] = True
        record["metadata"] = {k: registration[k] for k in ("created_by", "quote", "approval_id") if k in registration}
        engine.save(record)

        def before_accept(rec):
            backend = engine.backend
            if not callable(getattr(backend, "git", None)):
                raise NotImplementedError("F-18.3: intake snapshot commit backend required")
            root = Path(backend.checkout)
            if rec["sha"] != registration.get("candidate_sha") or backend.git("rev-parse", "HEAD") != rec["sha"] or backend.git("status", "--porcelain"):
                return False
            ident = registration["plugin_id"]
            manifest, graph = read_candidate(root, ident)
            for kind, body in (("manifest", manifest), ("graph", {"graph": graph, "manifest": manifest})):
                result = self.validate({"kind": kind, "body": deepcopy(body)})
                if result.get("ok") is not True or result.get("errors"):
                    return False
            actual, actual_graph = read_candidate(root, ident)
            if actual != manifest or actual_graph != graph:
                return False
            if not within_declaration(actual, registration["declaration"], registration["tier"]):
                return False
            paths = stage_registration(root, ident, actual, template_root=self.template_root)
            backend.git("add", "--", *paths)
            backend.git("commit", "-m", registration["message"], "--", *paths)
            rec["sha"] = backend.git("rev-parse", "HEAD")
            return True

        result = self.common.accept_branch(self.app, key, before_accept=before_accept)
        return {**result, "accepted": result.get("state") == "done"}


def bind_events(app, service, *, common=None):
    """Route only this destination's completed/ejected items through its checks.

    Use the existing common timeline poller. No additional consumer, listener,
    socket or loop is started. Ordinary work/core_change events keep their route.
    """
    if common is None:
        from core.intake.dest.b12 import common
    engine = common.engine(app)
    marker = getattr(engine, "_b12_subloop_service", None)
    if marker is service:
        return
    if marker is not None:
        raise RuntimeError("subloop event service already bound")
    original = engine.event

    def event(value):
        data = value.get("data") or {}
        if value.get("kind") == "item.state":
            record = engine.db.get("item:" + str(data.get("item_id")))
            if record and record["item"]["kind"] == "subloop_create":
                return service.on_item_state(value)
        return original(value)

    engine.event = event
    engine._b12_subloop_service = service
