"""席の差し込み口（b5: extra_listener_factories、register_via_factory）。"""
from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Callable, Mapping

from axcommon import timeutil
from core.intake.vias import Ident
from .listener import SeatListener
from .middleware import SeatMiddleware
from .service import RelayEvidence, SeatService
from .store import SeatError, SeatStore

__all__ = ["bind_effects", "register", "SeatStore", "SeatService", "SeatError", "RelayEvidence"]


EFFECTS = frozenset({"deny_pending", "release_controls", "release_claims", "close_sessions", "seat_sync"})


def bind_effects(service: SeatService, callbacks: Mapping[str, Callable[[str, str], None]] | None = None,
                 *, host_requests_dir: str | Path = "/state/host-requests") -> None:
    """Attach owned cleanup callbacks and the durable host ``seat-sync`` request."""
    for name, callback in (callbacks or {}).items():
        if name not in EFFECTS or not callable(callback):
            raise ValueError("invalid seat effect callback")
        service.effects[name] = callback
    if "seat_sync" not in service.effects:
        from core.observer.hostreq import write_request

        directory = Path(host_requests_dir)

        def seat_sync(_principal: str, _reason: str) -> None:
            write_request(directory, "seat-sync")

        service.effects["seat_sync"] = seat_sync


def register(app, service: SeatService, *, seat_socket: str | Path = "/run/seat/seat.sock",
             seat_gid: int | None = None, hq_socket: str | Path = "/run/hq-intake/intake.sock",
             hq_gid: int | None = None, gate_host: str | None = None,
             effects: Mapping[str, Callable[[str, str], None]] | None = None,
             hq_verifier: Callable[[dict, dict], object] | None = None,
             host_requests_dir: str | Path = "/state/host-requests"):
    """構成済みの部品を b5 へ登録する。uid/gid と profile は呼び元が解決する。

    gate_host は ax-core の intake の足。port は仕様どおり 8082。
    未設定の受け口だけを止める。承認のハンドラは登録しない。
    """
    state = app.state.intake
    app.state.seats = service
    bind_effects(service, effects or getattr(state, "seat_effects", None), host_requests_dir=host_requests_dir)
    if hq_verifier is not None:
        service.hq_verifier = hq_verifier
    app.add_middleware(SeatMiddleware, service=service, state=state)
    listeners = []

    def close_sessions(principal, reason):
        for listener in list(listeners):
            listener.close_principal(principal, reason)

    existing_close = service.effects.get("close_sessions")

    def close_all(principal, reason):
        close_sessions(principal, reason)
        if existing_close:
            existing_close(principal, reason)

    service.effects["close_sessions"] = close_all
    if state.audit is not None:
        service.audit = lambda ident, method, path, status: state.audit.write(
            ts=timeutil.now_ts(), via=ident.via, principal=ident.principal,
            method=method, path=path, status=status)

    def factory(name, via, path=None, group=None, host=None):
        def make(dispatcher):
            listener = SeatListener(service=service, name=name, dispatcher=dispatcher,
                                    unix_path=path, group_id=group, host=host,
                                    port=8082 if host else None, via_name=via)
            listeners.append(listener)
            return listener
        return make

    # 各口を別 factory にして、1 つの生成失敗が残りを妨げないようにする。
    state.extra_listener_factories.append(factory("seat", "ssh", str(seat_socket), seat_gid))
    raw_hq_uid = os.environ.get("AX_HQ_UID", "").strip()
    if raw_hq_uid.isdecimal():
        state.extra_listener_factories.append(factory("hq-intake", "hq", str(hq_socket), hq_gid))
    if gate_host:
        state.extra_listener_factories.append(factory("gate-intake", "tailnet", host=gate_host))
    else:
        service.store._alert("gate_intake_bind_missing")

    def revoke(app, dest, body, ident):
        try:
            row = service.revoke(body["payload"]["seat_id"], ident)
            return 200, {"seat_id": row["seat_id"], "state": row["state"]}
        except SeatError as error:
            return error.status, {"error": {"code": error.reason, "message": error.reason}}

    def add(app, dest, body, ident):
        approval_id = body.get("approved_via")
        if not isinstance(approval_id, str) or not approval_id:
            return 403, {"error": {"code": "forged_authority", "message": "forged_authority"}}
        try:
            row = service.add_ssh(body["payload"], approval_id)
            return 200, {"seat_id": row["seat_id"], "state": row["state"], "via": row["via"]}
        except SeatError as error:
            return error.status, {"error": {"code": error.reason, "message": error.reason}}
        except (KeyError, TypeError, ValueError):
            return 422, {"error": {"code": "bad_request", "message": "bad_request"}}

    # seat.add reaches this handler only through b12b2's verified approval executor.
    # The enrollment code response remains a separate one-use response path, so it
    # cannot enter the ordinary instruction/idempotency result store.
    state.dest_handlers["seat.revoke"] = revoke
    state.dest_handlers["seat.add"] = add
    return service
