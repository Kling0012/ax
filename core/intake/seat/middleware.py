"""既存の入口への判定の追加。HTTP の欄だけから資格を作らない。"""
from __future__ import annotations

import asyncio
import json

from axcommon import ids
from axcommon.http import json_error
from core.intake.vias import Ident
from .service import ENROLL_CODE, READS, SeatService
from .store import SeatError


class SeatMiddleware:
    def __init__(self, app, *, service: SeatService, state):
        self.app, self.service, self.state = app, service, state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        service = self.service
        via = scope.get("_seat_via")
        ident = scope.get("ax_ident")
        if via is None:
            # commander.sock の既存 uid 検査を経た身元だけを扱う。
            if not isinstance(ident, Ident):
                return await self.app(scope, receive, send)
            via = ident.via
        if via not in {"local", "hq", "ssh", "tailnet"}:
            return await self.app(scope, receive, send)
        request_id = ids.new_ulid()
        path, method = scope["path"], scope["method"]
        body = None
        headers = {}
        for key, value in scope.get("headers", []):
            key = key.decode("latin-1").lower()
            if key in headers:
                # 同じ資格欄を 2 度渡す曖昧な要求は拒否する。
                if key in {"x-ax-seat", "authorization"}:
                    return await json_error(403, "forged_authority")(scope, receive, send)
            headers[key] = value.decode("latin-1")
        declared = {k: headers.get("x-ax-" + k, "") for k in ("agent", "model", "session")}
        read = method == "GET"
        try:
            if via in {"ssh", "tailnet"}:
                allowed = method == "POST" and path == "/v1/instructions"
                allowed |= read and path.startswith("/v1/read/") and path[len("/v1/read/"):] in READS
                allowed |= method == "POST" and path == ("/v1/seat/session" if via == "ssh" else "/v1/seat/enroll")
                if method not in {"GET", "POST"}:
                    raise SeatError("method_not_allowed", 405)
                if not allowed:
                    raise SeatError("not_found", 404)
            if via == "ssh":
                ident = service.ssh(ident, headers.get("x-ax-seat", ""), read=read)
            elif via == "tailnet":
                evidence = service.relay_decoder({"headers": headers, "scope": scope})
                ident = service.relay(scope["_seat_peer"], evidence, enroll=path == "/v1/seat/enroll", read=read)
            else:
                if not isinstance(ident, Ident) or ident.via != via:
                    raise SeatError("forged_authority")
                ident = service.local_tier(ident)
            if method == "POST":
                pieces = []
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    pieces.append(message.get("body", b""))
                    if not message.get("more_body", False):
                        break
                raw = b"".join(pieces)
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise SeatError("bad_json", 400) from None
                if not isinstance(body, dict):
                    raise SeatError("bad_json", 400)
                if via == "tailnet" and path == "/v1/seat/enroll":
                    if set(body) != {"code"} or not isinstance(body.get("code"), str) or not ENROLL_CODE.fullmatch(body["code"]):
                        raise SeatError("bad_request", 400)
                    from starlette.responses import JSONResponse
                    return await JSONResponse(service.enroll(body["code"], evidence), headers={"X-AX-Request-Id": request_id})(scope, receive, send)
                if via == "ssh" and path == "/v1/seat/session":
                    raise NotImplementedError("S-6.13: session begin/end JSON fields unspecified")
                if path == "/v1/instructions":
                    from core.intake.validation import validate
                    self.state.registry.maybe_reload()
                    rejection = validate(self.state.registry, body, ident)
                    if rejection is None or rejection.status == 202:
                        preflight = getattr(self.state, "target_preflight", None)
                        target_error = await preflight(self.state, body) if preflight else None
                        if target_error:
                            status, reason = target_error
                            if self.state.sink is not None:
                                self.state.sink.emit("instruction.rejected", ids.new_ulid(),
                                    {"destination": body.get("destination"), "reason": reason},
                                    source_extra={"principal": ident.principal, "via": ident.via})
                            return await json_error(status, reason)(scope, receive, send)
                        service.assert_body(ident, body, declared, scope=scope)
                        if via == "hq":
                            ident = scope.get("ax_ident", ident)
                        service.check_available(ident, body)
                        service.count_instruction(ident)
                sent = False

                async def replay():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {"type": "http.request", "body": raw, "more_body": False}
                    return {"type": "http.disconnect"}

                downstream_receive = replay
            else:
                downstream_receive = receive
            scope = dict(scope, ax_ident=ident, ax_declared=declared)
            if scope.get("_seat_connection") is not None and ident is not None:
                scope["_seat_connection"].bind(ident)
            source = {"principal": ident.principal, "via": ident.via,
                      "session": scope.get("_seat_session", request_id), "declared": declared}
            service.emit("commander.request", ident.principal, source)

            async def wrapped_send(message):
                if message["type"] == "http.response.start":
                    message = dict(message)
                    message["headers"] = list(message.get("headers", [])) + [(b"x-ax-request-id", request_id.encode())]
                    status = message["status"]
                    if method == "POST" and path == "/v1/instructions":
                        service.count_result(ident, status >= 400)
                    connection = scope.get("_seat_connection")
                    if connection is not None:
                        connection.record(method, status)
                    if service.store and getattr(service, "audit", None):
                        service.audit(ident, method, path, status)
                await send(message)

            return await self.app(scope, downstream_receive, wrapped_send)
        except SeatError as error:
            if isinstance(ident, Ident) and ident.principal:
                if path == "/v1/instructions":
                    service.emit("instruction.rejected", ident.principal, {"reason": error.reason})
                    service.count_result(ident, True)
                service.emit("commander.denied", ident.principal, {"reason": error.reason})
            else:
                service.failed(via, error.reason)
            return await json_error(error.status, error.reason)(scope, receive, send)
        except NotImplementedError:
            return await json_error(503, "unavailable")(scope, receive, send)
        except Exception:
            service.store._alert("seat_request_failed")
            return await json_error(503, "unavailable")(scope, receive, send)
