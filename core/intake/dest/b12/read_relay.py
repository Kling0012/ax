"""S-6.7: commander の読み取りだけを中核の GET へ中継する。"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon.client import ApiClient, service_url
from axcommon.defaults import Defaults
from axcommon.timeutil import now_ts
from core.intake.vias import Ident, TIER_RANK

log = logging.getLogger("axintake.read_relay")
QUERY_KEYS = {
    "todos": ("state", "project", "subloop", "due"),
    "events": ("after", "kind", "subloop", "project", "subject", "limit"),
    "status": (),
    "brief": ("since", "project", "section", "new_session", "short", "offset"),
    "whoami": (),
    "guide": ("target", "version"),
    "destinations": (),
    "results": ("project", "job-id"),
}


class ReadRelay:
    def __init__(self, transport=None, *, timeout_s=None):
        defaults = Defaults.load()
        self.timeout_s = float(defaults.get("read_relay_timeout_s", override=timeout_s))
        # 読み取りのための資格は作らない。中核の網の 8080 だけを使う。
        self.clients = {
            name: ApiClient(service_url(name), transport=transport, timeout=self.timeout_s)
            for name in ("control", "timeline", "ledger")
        }

    async def _get(self, component, path, params=None):
        return await asyncio.wait_for(
            asyncio.to_thread(self.clients[component].get, path, params=params),
            timeout=self.timeout_s,
        )

    async def _component(self, component, requests):
        try:
            values = await asyncio.gather(*(
                self._get(component, path, params) for _, path, params in requests
            ))
            data = values[0] if requests[0][0] is None else {
                name: value for (name, _, _), value in zip(requests, values)
            }
            return component, {"ok": True, "data": data}
        except Exception:  # 中継先の失敗は、その部品だけ unavailable にする。
            return component, {"ok": False, "error": "unavailable"}

    async def read(self, op, query):
        if op not in QUERY_KEYS:
            raise KeyError(op)
        params = {k: query[k] for k in QUERY_KEYS[op] if k in query}
        if op == "status":
            plans = {
                "control": [("plugins", "/v1/plugins", None),
                            ("commands", "/v1/commands", {"state": "active"})],
                "timeline": [(name, "/v1/projections/" + quote(name), None)
                             for name in ("plugins", "runs", "controls")],
            }
        elif op == "todos":
            plans = {"ledger": [(None, "/v1/todos", params)]}
        else:
            plans = {"timeline": [(None, "/v1/events", params)]}
        return dict(await asyncio.gather(*(
            self._component(component, requests) for component, requests in plans.items()
        )))


def register(app):
    """b12 の register(app) から呼ぶ。既存の B5 の口・監査ログを使う。"""
    if getattr(app.state, "b12_read_relay", None) is not None:
        return
    st = app.state.intake
    relay = ReadRelay(st.transport)
    app.state.b12_read_relay = relay

    async def read(request):
        ident = request.scope.get("ax_ident")
        op = request.url.path.rsplit("/", 1)[-1]
        if not isinstance(ident, Ident) or ident.from_ != "commander" or ident.via not in {"local", "ssh", "tailnet"}:
            status, body = 403, {"error": {"code": "receptacle_not_allowed"}}
        elif ident.max_tier not in TIER_RANK:
            status, body = 403, {"error": {"code": "tier_insufficient"}}
        elif op not in QUERY_KEYS:
            status, body = 404, {"error": {"code": "not_found"}}
        elif request.method != "GET":
            status, body = 405, {"error": {"code": "method_not_allowed"}}
        elif op == "brief":
            from .brief import brief
            status, body = await brief(app, relay, ident, request.query_params, request.scope.get("ax_declared"))
        elif op == "destinations":
            # Use the existing endpoint so its registry and seat filtering remain authoritative.
            destination = next((route for route in app.router.routes
                                if getattr(route, "path", None) == "/v1/destinations"
                                and "GET" in getattr(route, "methods", set())), None)
            if destination is None:
                status, body = 503, {"error": {"code": "unavailable"}}
            else:
                return await destination.endpoint(request)
        elif op == "results":
            project = request.query_params.get("project")
            job_id = request.query_params.get("job-id")
            if not project or not job_id:
                status, body = 400, {"error": {"code": "bad_request"}}
            else:
                try:
                    engine = app.state.b12
                    with engine.lock:
                        records = [r for r in engine.db.work_requests() if r.get("job_id") == job_id]
                    if len(records) != 1:
                        status, body = (404 if not records else 409), {"error": {"code": "not_found" if not records else "ambiguous_target"}}
                    elif records[0].get("owner") != ident.principal or not ident.principal:
                        status, body = 403, {"error": {"code": "not_owner"}}
                    elif records[0].get("payload", {}).get("project") != project:
                        status, body = 404, {"error": {"code": "not_found"}}
                    else:
                        body = await asyncio.wait_for(asyncio.to_thread(engine.pool.get, "/v1/jobs/" + quote(job_id, safe="")), timeout=relay.timeout_s)
                        status = 200
                except Exception:
                    status, body = 503, {"error": {"code": "unavailable"}}
        elif op == "whoami":
            body = {"seat_id": ident.principal, "tier": ident.max_tier, "via": ident.via}
            if ident.via in {"ssh", "tailnet"}:
                try:
                    row = app.state.seats.store.get(ident.principal)
                    body.update({key: row[key] for key in ("device", "device_id", "expires_at") if key in row})
                except Exception:
                    status, body = 503, {"error": {"code": "seat_unavailable"}}
                else:
                    status = 200
            else:
                status = 200
        elif op == "guide":
            try:
                from tools.gen_commander_guide import load_bundle
                bundle = load_bundle()
                target = request.query_params.get("target", "agents")
                if target not in bundle.targets and target != "stdout":
                    status, body = 400, {"error": {"code": "bad_request"}}
                else:
                    body = {"guide_version": bundle.guide_version}
                    if request.query_params.get("version") != "1":
                        body["guide"] = bundle.render(target)
                    status = 200
            except Exception:
                status, body = 503, {"error": {"code": "guide_unavailable"}}
        else:
            status, body = 200, await relay.read(op, request.query_params)
        if st.audit is not None:
            try:
                st.audit.write(ts=now_ts(), via=getattr(ident, "via", None),
                               principal=getattr(ident, "principal", None),
                               method=request.method, path=request.url.path, status=status)
            except Exception:
                log.warning("読み取りの監査ログを書けない")
        headers = {"Allow": "GET"} if status == 405 else None
        return JSONResponse(body, status_code=status, headers=headers)

    # Starlette の GET は HEAD も許すため、メソッドの判定を handler 内で行う。
    for op in QUERY_KEYS:
        app.router.routes.append(Route("/v1/read/" + op, read,
                                       methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"]))
