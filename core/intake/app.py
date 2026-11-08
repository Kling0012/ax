"""入口のアプリ（S-6.1〜S-6.5、D-5）。

POST /v1/instructions と GET /v1/destinations を、commander ソケット（F-9.2）で受ける。
healthz 用の TCP（AX_HEALTH_PORT）は `build_health_app` の別アプリ（指示の口は載せない。
内部コンテナが指示を出せる口を増やさない。S-15.5）。

後続の束の差し込み口（PLAN.json の conventions）:
- `core/intake/ext/<pkg>.py` と `core/intake/dest/<pkg>/__init__.py` の register(app)（axcommon.ext と
  load_dest_packages が読む。失敗はその拡張だけを止める）
- `app.state.intake.conflict_guard`（S-6.17 の衝突の検出。b12b3）
- `app.state.intake.approval_check`（S-6.10 の承認。b12b2）
- `app.state.intake.extra_listener_factories`（seat.sock・hq ソケット・gate 用の TCP。b12b1）
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from axcommon import ext as ext_mod
from axcommon import ids, timeutil
from axcommon.client import ApiClient, ApiError, Transport, Unavailable
from axcommon.events import EventSink
from axcommon.http import create_app, json_error

from . import __version__
from .audit import AccessLog
from .idempotency import DONE, PENDING, IdempotencyStore
from .registry import Registry, repo_config_dir, unresolved_contract
from .validation import Rejection, validate
from .vias import Ident, TIER_RANK
from starlette.responses import JSONResponse

log = logging.getLogger("axintake.app")

DEST_HANDLER = Callable[[Any, dict, dict, Ident], "tuple[int, Any]"]


class NotRoutable(Exception):
    """handler の URL が渡し先として使えない（コードの handler も無い）。"""


def api_from_handler(handler_url: str, *, transport: Transport | None = None) -> tuple[ApiClient, str]:
    """登録簿の handler の URL を (ApiClient, path) に分ける。http(s) 之外は NotRoutable。"""
    from urllib.parse import urlsplit

    u = urlsplit(handler_url or "")
    if u.scheme not in ("http", "https") or not u.netloc:
        raise NotRoutable(f"handler が http(s) の URL でない: {handler_url!r}")
    return ApiClient(f"{u.scheme}://{u.netloc}", transport=transport), u.path or "/"


def route_instruction(app: Any, dest: dict, body: dict, ident: Ident, declared: dict | None = None) -> tuple[int, Any]:
    """行き先へ渡す（コードの handler があればそれ、無ければ handler の URL への素の POST）。"""
    st: IntakeState = app.state.intake
    handler = st.dest_handlers.get(dest["name"])
    try:
        if handler is not None:
            if dest["name"] == "claim.op" and callable(getattr(handler, "handle_with_context", None)):
                return handler.handle_with_context(app, dest, body, ident, declared or {})
            return handler(app, dest, body, ident)
        client, path = api_from_handler(dest.get("handler", ""), transport=st.transport)
        return 200, client.post(path, body.get("payload"))
    except ApiError as e:
        body_out = e.body if isinstance(e.body, dict) and e.body else {"error": {"code": e.code, "message": e.message}}
        return e.status, body_out


@dataclass
class IntakeState:
    registry: Registry
    transport: Transport | None = None
    sink: EventSink | None = None
    idem: IdempotencyStore | None = None
    audit: AccessLog | None = None
    dest_handlers: dict[str, DEST_HANDLER] = field(default_factory=dict)
    conflict_guard: Callable[[dict, dict, Ident], Rejection | None] | None = None   # b12b3
    approval_check: Callable[[dict, dict, Ident], Rejection | None] | None = None   # b12b2
    extra_listener_factories: list[Callable] = field(default_factory=list)          # b12b1
    listener_status: dict[str, dict] = field(default_factory=dict)                  # healthz 用
    executor: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=2, thread_name_prefix="axintake-dest"))
    dest_results: dict[str, str | None] = field(default_factory=dict)


def load_dest_packages(app: Any, dest_dir: str | Path | None) -> dict[str, str | None]:
    """core/intake/dest/<pkg>/ の register(app) を名前順に読む（差し込み口）。

    各ディレクトリは `__init__.py` に register(app) を持つ。1 つの失敗はその行き先だけを止める
    （fail-soft）。healthz の ext_failed にも出す。
    """
    if dest_dir is None:
        return {}
    results: dict[str, str | None] = {}
    d = Path(dest_dir)
    if not d.is_dir():
        return results
    for sub in sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        init = sub / "__init__.py"
        if not init.is_file():
            continue
        mod_name = f"axintake_dest_{sub.name}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, init)
            if spec is None or spec.loader is None:
                raise ImportError("spec を作れない")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            reg = getattr(mod, "register", None)
            if not callable(reg):
                raise AttributeError("register(app) が無い")
            reg(app)
            results[sub.name] = None
        except Exception as e:  # noqa: BLE001  拡張 1 件の失敗で入口を止めない
            sys.modules.pop(mod_name, None)
            results[sub.name] = type(e).__name__
            log.error("行き先パッケージ %s の読み込みに失敗: %s", sub.name, type(e).__name__)
            fails = getattr(app.state, "ext_failures", None)
            if isinstance(fails, list):
                fails.append(f"dest:{sub.name}")
    return results


def _emit(st: IntakeState, kind: str, subject: str, data: dict | None, ident: Ident | None = None,
          session: str | None = None, corr: str | None = None) -> dict | None:
    """時系列記録へ（S-6.1: 拒否した指示と失敗した認証も残す）。送れなくても止めない（EventSink が溜める）。"""
    if st.sink is None:
        return None
    source_extra: dict[str, Any] = {}
    if ident is not None:
        source_extra = {"principal": ident.principal, "via": ident.via}
    if session:
        source_extra["session"] = session[:64]
    try:
        return st.sink.emit(kind, subject, data, corr=corr, source_extra=source_extra or None)
    except Exception:  # noqa: BLE001
        log.exception("事象 %s を作れない", kind)
        return None


def health_alert(st: IntakeState, check: str, detail: str = "", *, severity: str = "warning") -> None:
    """`health.alert`（data は check/target/severity。schemas/event-data.schema.json の形）。"""
    data: dict[str, Any] = {"check": check, "severity": severity}
    if detail:
        data["target"] = str(detail)[:200]
    _emit(st, "health.alert", "intake", data)


def health_extra(st: IntakeState) -> Callable[[], dict]:
    def extra() -> dict:
        out: dict[str, Any] = {"destinations": len(st.registry.names())}
        faults = sorted(set(st.registry.faults))
        if faults:
            out["registry_faults"] = faults
        if st.listener_status:
            out["listeners"] = {k: dict(v) for k, v in st.listener_status.items()}
            if any(not v.get("listening") for v in st.listener_status.values()):
                # 待ち受けが済むと /healthz が ok になる（F-9.2）。逆に、待ち受けられないときは ok でない
                out["ok"] = False
        return out

    return extra


def build_app(*, config_dir: str | Path | None = None, dest_dir: str | Path | None = None,
              ext_dir: str | Path | None = None, transport: Transport | None = None,
              sink: EventSink | None = None, idem: IdempotencyStore | None = None,
              audit: AccessLog | None = None, version: str = __version__,
              run_heartbeat_loop: bool = True):
    """入口のアプリ。試験は transport に tests/fakes/http.py の FakeTransport を渡す。"""
    cdir = Path(config_dir) if config_dir else repo_config_dir()
    registry = Registry.load(cdir)
    if dest_dir is None:
        dest_dir = Path(__file__).resolve().parent / "dest"
    if ext_dir is None:
        ext_dir = Path(__file__).resolve().parent / "ext"
    if idem is None:
        from axcommon.defaults import Defaults

        idem = IdempotencyStore(None, retain_h=float(Defaults.load().get("idempotency_retain_h")))
    st = IntakeState(registry=registry, transport=transport, sink=sink, idem=idem, audit=audit)

    # ---- POST /v1/instructions ----
    async def instructions(request):
        ident = request.scope.get("ax_ident")
        if not isinstance(ident, Ident):
            _emit(st, "auth.failed", "intake", {"via": str(request.scope.get("ax_via") or "?"), "reason": "no_ident"})
            return json_error(403, "auth.failed", "受け口の資格が確かめられない")
        reload_registry()
        try:
            body = json.loads(await request.body() or b"")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            _audit(ident, "POST", "/v1/instructions", 400)
            return json_error(400, "bad_json", "本文が JSON のオブジェクトでない")
        body.setdefault("id", ids.new_ulid())
        session = body.get("session") if isinstance(body.get("session"), str) else None
        instruction_id = body["id"]
        _emit(st, "instruction.received", instruction_id, {"destination": body.get("destination")},
              ident=ident, session=session, corr=instruction_id)

        rejection = validate(st.registry, body, ident)
        if rejection is not None and rejection.status != 202:
            return _reject(rejection, ident, body, session)
        if rejection is not None and rejection.reason == "approval_required":
            # 承認の保存（b12b2）の差し込み口が無い間は、承認待ちとして受けるだけにする
            if st.approval_check is not None:
                try:
                    r2 = await asyncio.to_thread(st.approval_check, body, st.registry.get(body["destination"]), ident)
                except Exception:  # noqa: BLE001
                    log.exception("承認の判定に失敗")
                    r2 = Rejection(503, "approval_unavailable", "承認の判定に失敗した")
                if r2 is not None:
                    return _reject(r2, ident, body, session)
            _audit(ident, "POST", "/v1/instructions", 202)
            return _json(202, {"ok": False, "reason": "approval_required", "instruction_id": instruction_id,
                               "destination": body.get("destination")})

        dest = st.registry.get(body["destination"])

        # idempotency_key の照合（S-6.16）
        key = body.get("idempotency_key")
        replayed = False
        if key:
            rec = st.idem.lookup(dest["name"], key, body.get("payload"))
            if rec is not None:
                if rec.get("mismatch"):
                    return _reject(Rejection(409, "idempotency_mismatch", "同じ依頼 ID で違う本文"),
                                   ident, body, session)
                if rec.get("state") == DONE:
                    replayed = True
                    result = rec.get("result")
                    status = rec.get("status") or 200
                    _audit(ident, "POST", "/v1/instructions", 200)
                    return _json(200, {"ok": True, "instruction_id": instruction_id, "destination": dest["name"],
                                       "status": status, "result": result, "replayed": True})
                # PENDING: 二重に処理しない（S-6.16）
                _audit(ident, "POST", "/v1/instructions", 202)
                return _json(202, {"ok": True, "instruction_id": instruction_id, "destination": dest["name"],
                                   "status": "pending", "replayed": True})

        # 衝突の検出（S-6.17。b12b3 の差し込み口）
        if st.conflict_guard is not None:
            try:
                r3 = await asyncio.to_thread(st.conflict_guard, body, dest, ident)
            except Exception:  # noqa: BLE001
                log.exception("衝突の判定に失敗")
                r3 = Rejection(503, "conflict_unavailable", "衝突の判定に失敗した")
            if r3 is not None:
                return _reject(r3, ident, body, session)

        if key:
            st.idem.begin(dest["name"], key, body.get("payload"), ident.principal)

        async_dest = dest.get("ack") == "async"
        if async_dest:
            def _run() -> None:
                try:
                    status, result = route_instruction(request.app, dest, body, ident, request.scope.get("ax_declared"))
                    st.idem.finish(dest["name"], key, status=status, result=result)
                    _emit(st, "instruction.routed", instruction_id,
                          {"destination": dest["name"], "status": status}, ident=ident, session=session,
                          corr=instruction_id)
                except Unavailable:
                    _emit(st, "instruction.rejected", instruction_id,
                          {"destination": dest["name"], "reason": "handler_unavailable"}, ident=ident,
                          session=session, corr=instruction_id)
                except NotRoutable as e:
                    _emit(st, "instruction.rejected", instruction_id,
                          {"destination": dest["name"], "reason": "handler_not_routable"}, ident=ident,
                          session=session, corr=instruction_id)
                    log.error("行き先 %s: %s", dest["name"], e)
                except Exception:  # noqa: BLE001
                    log.exception("行き先 %s の処理に失敗", dest["name"])
                    _emit(st, "instruction.rejected", instruction_id,
                          {"destination": dest["name"], "reason": "handler_failed"}, ident=ident,
                          session=session, corr=instruction_id)

            st.executor.submit(_run)
            _audit(ident, "POST", "/v1/instructions", 202)
            return _json(202, {"ok": True, "instruction_id": instruction_id, "destination": dest["name"],
                               "status": "pending", "replayed": False})

        try:
            status, result = await asyncio.to_thread(route_instruction, request.app, dest, body, ident, request.scope.get("ax_declared"))
        except Unavailable:
            return _reject(Rejection(503, "handler_unavailable", "行き先が応答しない"), ident, body, session)
        except NotRoutable as e:
            log.error("行き先 %s: %s", dest["name"], e)
            return _reject(Rejection(501, "handler_not_routable", "渡す先が決められていない"), ident, body, session)
        except Exception:  # noqa: BLE001
            log.exception("行き先 %s の処理に失敗", dest["name"])
            return _reject(Rejection(500, "handler_failed", "行き先の処理に失敗した"), ident, body, session)

        if key:
            st.idem.finish(dest["name"], key, status=status, result=result)
        if status < 400:
            _emit(st, "instruction.routed", instruction_id, {"destination": dest["name"], "status": status},
                  ident=ident, session=session, corr=instruction_id)
        else:
            _emit(st, "instruction.rejected", instruction_id,
                  {"destination": dest["name"], "reason": "handler_rejected", "status": status},
                  ident=ident, session=session, corr=instruction_id)
        _audit(ident, "POST", "/v1/instructions", status)
        return _json(status, {"ok": status < 400, "instruction_id": instruction_id, "destination": dest["name"],
                              "status": status, "result": result, "replayed": replayed})

    # ---- GET /v1/destinations（S-6.15 の destinations の部分。reads は b12b3）----
    async def destinations(request):
        ident = request.scope.get("ax_ident")
        if not isinstance(ident, Ident):
            return json_error(403, "auth.failed", "受け口の資格が確かめられない")
        reload_registry()
        out = []
        for name in st.registry.names():
            d = st.registry.get(name)
            if ident.via not in d["via"]:
                continue
            if TIER_RANK.get(d["tier"], 9) > TIER_RANK.get(ident.max_tier, 0):
                continue
            item = {"name": name, "title": d.get("title", ""), "schema": d.get("schema", ""), "tier": d["tier"]}
            if d.get("mcp_tool"):
                item["mcp_tool"] = d["mcp_tool"]
            if reason := unresolved_contract(d):
                item["x-ax-unresolved"] = reason
            out.append(item)
        _audit(ident, "GET", "/v1/destinations", 200)
        return _json(200, {"destinations": out, "reads": []})

    def reload_registry() -> None:
        try:
            st.registry.maybe_reload(
                emit_alert=lambda reason, data: health_alert(st, reason, ",".join(data.get("faults", []))))
        except Exception:  # noqa: BLE001  読み直しの失敗で指示を止めない
            log.exception("登録簿の読み直しに失敗（前の登録簿を使い続ける）")

    def _audit(ident: Ident, method: str, path: str, status: int) -> None:
        if st.audit is not None:
            st.audit.write(ts=timeutil.now_ts(), via=ident.via, principal=ident.principal,
                           method=method, path=path, status=status)

    def _reject(rejection: Rejection, ident: Ident, body: dict, session: str | None):
        _emit(st, "instruction.rejected", body.get("id", "-"),
              {"destination": body.get("destination"), "reason": rejection.reason},
              ident=ident, session=session, corr=body.get("id"))
        _audit(ident, "POST", "/v1/instructions", rejection.status)
        return json_error(rejection.status, rejection.reason, rejection.message)

    def _json(status: int, payload: dict):
        return JSONResponse(payload, status_code=status)

    from starlette.routing import Route

    app = create_app("intake", version, routes=[
        Route("/v1/instructions", instructions, methods=["POST"]),
        Route("/v1/destinations", destinations, methods=["GET"]),
    ], sink=sink, extra_health=health_extra(st), ext_dir=None, run_heartbeat_loop=run_heartbeat_loop)
    app.state.intake = st
    app.state.ext_results = ext_mod.load_extensions(app, ext_dir)
    st.dest_results = load_dest_packages(app, dest_dir)
    if registry.faults:
        health_alert(st, "intake_registry_faults", ",".join(sorted(set(registry.faults))))
    return app


def build_health_app(state: IntakeState, *, version: str = __version__):
    """healthz 用の TCP（AX_HEALTH_PORT）に載せるアプリ。指示の口は持たない（S-15.5）。"""
    return create_app("intake", version, sink=None, extra_health=health_extra(state),
                      run_heartbeat_loop=False)


def service_url(name: str, port: int = 8080) -> str:
    from axcommon.client import service_url as _su

    return _su(name, port)


def host_name() -> str:
    import socket

    return os.environ.get("AX_HOST") or socket.gethostname()


def outbox_dir() -> Path:
    base = os.environ.get("AX_STATE_DIR") or "/state"
    return Path(base) / "outbox" / "intake"


def idem_path() -> Path:
    base = os.environ.get("AX_STATE_DIR") or "/state"
    return Path(base) / "intake" / "idempotency.jsonl"


def commander_dir() -> Path:
    return Path(os.environ.get("AX_COMMANDER_DIR") or "/run/commander")
