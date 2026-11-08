"""Explicit B5 registration. No automatic ext module is created outside ownership."""
from __future__ import annotations

import asyncio
import json

from axcommon import schemas
from core.intake.vias import Ident
from .store import ApprovalError


def register(app, service, *, hq_verifier=None):
    """hq_verifier(scope, body)->VerifiedUser is supplied by HQ's wire adapter.

    No default accepts arbitrary booleans from a body. The specification does not
    name the verification-result wire fields, so that binding must be explicit.
    """
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def reply(request):
        ident = request.scope.get("ax_ident")
        status = 200
        try:
            if not isinstance(ident, Ident) or ident.via != "hq":
                raise ApprovalError(403, "receptacle_not_allowed")
            body = await request.json() if request.method == "POST" else {}
            if request.method == "POST":
                if not isinstance(body, dict) or not isinstance(body.get("digest"), str):
                    raise ApprovalError(422, "bad_request")
                if hq_verifier is None:
                    raise NotImplementedError("S-6.10: HQ verification-result wire binding missing")
                proof = hq_verifier(request.scope, body)
                ident = request.scope.get("ax_ident", ident)
                service._hq(ident, proof, factor=True)
            elif (ident.from_, ident.principal) != ("hq", "user"):
                raise ApprovalError(403, "receptacle_not_allowed")
            approval_id = request.path_params.get("approval_id")
            if request.method == "POST":
                data = await asyncio.to_thread(service.grant, approval_id, body["digest"], ident, proof)
            elif approval_id:
                data = await asyncio.to_thread(service.view, approval_id)
            else:
                data = {"approvals": [service.view(r["approval_id"])["data"] for r in service.store.list()]}
            response = JSONResponse(data)
        except ApprovalError as exc:
            status = exc.status
            response = JSONResponse({"error": {"code": exc.reason, "message": exc.reason, "hint": ""}}, status_code=status)
            if isinstance(ident, Ident):
                if exc.reason in {"unauthorized", "bad_code", "peer_unknown", "locked"}:
                    service.audit.auth_failed(ident, exc.reason)
                service.audit.denied(ident, "approval.grant" if request.method == "POST" else "approvals", exc.reason)
        except (ValueError, TypeError):
            status = 422
            response = JSONResponse({"error": {"code": "bad_request", "message": "bad_request", "hint": ""}}, status_code=status)
        except NotImplementedError:
            status = 501
            response = JSONResponse({"error": {"code": "approval_binding_missing", "message": "approval_binding_missing", "hint": ""}}, status_code=status)
        if isinstance(ident, Ident):
            service.audit.request(ident, request.method, request.url.path, status)
        return response

    async def snapshot(request):
        ident = request.scope.get("ax_ident")
        if not isinstance(ident, Ident) or (ident.via, ident.from_, ident.principal) != ("hq", "hq", "user"):
            return JSONResponse({"error": {"code": "receptacle_not_allowed"}}, status_code=403)
        try:
            return JSONResponse({"approvals": await asyncio.to_thread(service.pending_snapshot)})
        except (ValueError, TypeError):
            return JSONResponse({"error": {"code": "approval_bad_state"}}, status_code=503)

    app.router.routes.extend([
        Route("/v1/approval-snapshot", snapshot, methods=["GET"]),
        Route("/v1/approvals", reply, methods=["GET"]),
        Route("/v1/approvals/{approval_id}", reply, methods=["GET"]),
        Route("/v1/approvals/{approval_id}/grant", reply, methods=["POST"]),
    ])
    app.state.approvals = service
    from .change_approval import register_change_approval
    register_change_approval(app, service)
    if service.executor is None:
        import threading
        delivery = threading.local()
        supported = {"core.change", "subloop.create", "secret.push", "seat.add", "seat.enroll_start",
                     "seat.lock", "seat.tier", "seat.unlock"}

        def deliver_code(approval_id, code):
            if getattr(delivery, "approval_id", None) != approval_id or not isinstance(code, str):
                raise ValueError("invalid enrollment delivery")
            delivery.code = code

        app.state.seat_enrollment_delivery = deliver_code

        def execute_saved(command):
            name = command.get("destination")
            if name not in supported or command.get("issued_by") != "user":
                raise NotImplementedError("approved destination executor unavailable")
            if name == "core.change":
                from core.intake.dest.b12.core_change import execute_approved
                return execute_approved(app, command)
            if name == "subloop.create":
                from core.intake.dest.b12b3.subloop import execute_approved
                return execute_approved(app, command)
            handler = app.state.intake.dest_handlers.get(name)
            if handler is None:
                raise NotImplementedError("approved destination handler unavailable")
            ident = Ident("hq", "hq", "user", "irreversible")
            delivery.approval_id = command.get("approved_via")
            delivery.code = None
            try:
                status, result = handler(app, {"name": name}, command, ident)
                if status >= 400:
                    return status, result
                if name == "seat.enroll_start":
                    if not delivery.code:
                        raise ValueError("enrollment delivery unavailable")
                    return status, {"enrollment_code": delivery.code}
                return status, result
            finally:
                delivery.approval_id = None
                delivery.code = None

        def supports(name):
            if name not in supported:
                return False
            if name == "core.change":
                return True
            if name == "subloop.create":
                return getattr(app.state, "b12_subloop", None) is not None
            if name.startswith("seat.") and getattr(app.state, "seats", None) is None:
                return False
            return name in app.state.intake.dest_handlers

        execute_saved.supports = supports
        service.executor = execute_saved
    from .audit import AuditContextMiddleware, VerifiedIntakeSink
    if app.state.intake.sink is not None:
        app.state.intake.sink = VerifiedIntakeSink(app.state.intake.sink, service.audit)
    app.add_middleware(ApprovalMiddleware, service=service, registry=app.state.intake.registry,
                       conflict_guard=lambda: app.state.intake.conflict_guard, hq_verifier=hq_verifier)
    app.add_middleware(AuditContextMiddleware)
    return service


class ApprovalMiddleware:
    """B5's approval_check cannot supply an approval_id response; intercept early."""
    def __init__(self, app, *, service, registry, hq_verifier=None, conflict_guard=None):
        self.app, self.service, self.registry = app, service, registry
        self.hq_verifier = hq_verifier
        self.conflict_guard = conflict_guard

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != "/v1/instructions":
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse
        ident = scope.get("ax_ident")
        if not isinstance(ident, Ident):
            # Let the intake entry point emit its normal authentication failure.
            await self.app(scope, receive, send)
            return
        chunks = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        raw = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return await receive()

        body = {}
        handled, status, output = False, 200, None
        try:
            try:
                body = json.loads(raw)
            except ValueError:
                await self.app(scope, replay, send)
                return
            if not isinstance(body, dict):
                await self.app(scope, replay, send)
                return
            self.registry.maybe_reload()
            from core.intake.validation import validate
            rejection = validate(self.registry, body, ident)
            if rejection is not None and rejection.status != 202:
                await self.app(scope, replay, send)
                return
            dest = self.registry.get(body.get("destination"))
            if not dest:
                raise ApprovalError(404, "unknown_destination")
            from axcommon.hq_proof import requires_proof
            if ident.via == "hq" and requires_proof(body.get("destination")):
                if self.hq_verifier is None:
                    raise NotImplementedError("HQ proof binding missing")
                proof = self.hq_verifier(scope, body)
                ident = scope.get("ax_ident", ident)
                self.service._hq(ident, proof, factor=False)
            effective = self.service.policy.check(body, dest, ident)
            name = dest["name"]
            if name in {"approval.grant", "approval.deny", "approval.withdraw"}:
                payload = body["payload"]
                aid = payload.get("approval_id")
                if name == "approval.withdraw":
                    output = await asyncio.to_thread(self.service.withdraw, aid, ident)
                else:
                    if self.hq_verifier is None:
                        raise NotImplementedError("S-6.10: HQ proof binding missing")
                    proof = self.hq_verifier(scope, body)
                    if name == "approval.grant":
                        output = await asyncio.to_thread(self.service.grant, aid, payload.get("digest"), ident, proof)
                    else:
                        output = await asyncio.to_thread(self.service.deny, aid, ident, proof)
                handled = True
            elif (effective["tier"] == "irreversible"
                  and not (ident.via == "hq" and body.get("confirm_irreversible") is True
                           and name in {"permission.change", "control.command"})):
                self.service.audit.emit("instruction.received", "intake", ident, data={"destination": name})
                guard = self.conflict_guard() if self.conflict_guard else None
                if guard:
                    rejected = await asyncio.to_thread(guard, body, effective, ident)
                    if rejected:
                        raise ApprovalError(rejected.status, rejected.reason)
                if self.service.policy.permitted(body, ident):
                    if self.service.executor is None:
                        raise NotImplementedError("D-5.1: executor credential binding missing")
                    # The trusted permission matcher, never caller text, authorizes this path.
                    command = {k: body.get(k) for k in ("destination", "payload", "project", "session", "expires_at")}
                    command.update(issued_by="commander", approved_via=None, confirm_irreversible=False)
                    output = await asyncio.to_thread(self.service.executor, command)
                    handled = True
                else:
                    result = await asyncio.to_thread(self.service.submit, body, dest, ident)
                    status, output, handled = 202, {"reason": "approval_required", "state": result["status"], **result}, True
        except ApprovalError as exc:
            handled, status = True, exc.status
            output = {"error": {"code": exc.reason, "message": exc.reason, "hint": ""}}
            if isinstance(ident, Ident):
                self.service.audit.denied(ident, body.get("destination") if isinstance(body, dict) else None,
                                          exc.reason, session=body.get("session") if isinstance(body, dict) else None)
        except (ValueError, TypeError):
            handled, status, output = True, 422, {"error": {"code": "bad_request", "message": "bad_request", "hint": ""}}
        except NotImplementedError:
            handled, status, output = True, 501, {"error": {"code": "approval_binding_missing", "message": "approval_binding_missing", "hint": ""}}
        if handled:
            if isinstance(ident, Ident):
                self.service.audit.request(ident, "POST", "/v1/instructions", status)
            await JSONResponse(output, status_code=status)(scope, receive, send)
            return

        await self.app(scope, replay, send)
