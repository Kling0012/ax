"""B12-1 calls register(app); shared destinations and parent stay untouched."""
from __future__ import annotations

from .policy import Rejected, decide, within_declaration
from .service import Approval, Ports, Request, Service
from .store import Store

__all__ = ["Approval", "Ports", "Request", "Service", "Store", "decide", "within_declaration", "register"]


def register(app) -> None:
    """Install only subloop.create. Other destinations/guards are never wrapped.

    B12-1 supplies app.state.b12_subloop and a wire decoder once S-6.22's
    unresolved assembly payload contract has been settled. This package does
    not infer ids, titles or project names from arbitrary request JSON.
    """
    config = getattr(app.state, "b12_subloop_config", None)
    if config is not None:
        from .assembly import assemble
        try:
            service = assemble(app, **config)
            from core.intake.dest.b12b3.subloop import register as register_wire
            register_wire(app, service)
        except Exception:
            app.state.intake.dest_handlers["subloop.create"] = unavailable
        return

    def handler(app, dest, body, ident):
        service = getattr(app.state, "b12_subloop", None)
        decoder = getattr(app.state, "b12_subloop_decode", None)
        if service is None or decoder is None:
            return unavailable(app, dest, body, ident)
        payload = body.get("payload")
        if not isinstance(payload, dict):
            return 400, {"reason": "spec_declaration_required"}
        try:
            # Do documented checks before delegating the unresolved part.
            decide(payload.get("spec"), payload.get("quote"), ident)
        except Rejected as exc:
            return exc.status, {"reason": exc.reason}
        request = decoder(payload)
        if not isinstance(request, Request) or request.spec != payload.get("spec") or request.quote != payload.get("quote"):
            raise ValueError("wire decoder changed the declared authority inputs")
        return service.create(body.get("idempotency_key"), request, ident)

    app.state.intake.dest_handlers["subloop.create"] = handler
    service = getattr(app.state, "b12_subloop", None)
    if service is not None and getattr(app.state, "b12", None) is not None:
        from .bridge import bind_events
        bind_events(app, service)


def unavailable(app, dest, body, ident):
    return 503, {"error": {"code": "subloop_unavailable", "message": "subloop service is not configured"}}
