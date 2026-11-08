"""HQ common application, request boundary and extension registration."""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from axcommon.ext import load_extensions
from axcommon.http import create_app as common_app, json_error
from .api import register as register_api
from .auth import AuthFailure, single_header, source_address
from .secrets import SecretFailure
from .settings import Settings
from .intake import IntakeClient


class ReadState:
    """Each resource retains its own last-success time; failures are never samples."""
    def __init__(self, *, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        self.last = {}

    async def fetch(self, resource, loader, *, sample=False):
        try:
            result = loader()
            if inspect.isawaitable(result):
                result = await result
            at = self.clock()
            if not sample:
                self.last[resource] = at
            return {"status": "sample" if sample else "ok", "sample": sample, "data": result,
                    "last_success_at": self.last.get(resource)}
        except Exception:
            return {"status": "unavailable", "sample": False, "data": None,
                    "last_success_at": self.last.get(resource)}


class HQBoundary:
    def __init__(self, app, *, owner):
        self.app, self.owner = app, owner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        request = Request(scope, receive=receive)
        settings = self.owner.state.hq_settings
        path = scope["path"].rstrip("/") or "/"
        secret_path = path == "/v1/secrets" or path.startswith("/v1/secrets/") or path == "/api/secrets" or path.startswith("/api/secrets/")
        private = path == "/seats" or path.startswith(("/seats/", "/approval/", "/approvals/", "/v1/hq/approval", "/v1/hq/seats", "/v1/hq/b12b2/"))
        try:
            _, public = source_address(request, settings.public_proxy_ip)
            if public and private:
                return await json_error(404, "not_found")(scope, receive, send)
            if public and secret_path:
                raise AuthFailure("secret_local_only")
            if public and path in {"/v1/hibi/now", "/v1/hibi/day"}:
                raise AuthFailure("local_only")
            who = self.owner.state.hq_auth(request)
            if inspect.isawaitable(who):
                who = await who
            if not isinstance(who, dict) or not who or who.get("allowed") is False:
                raise AuthFailure(getattr(request.state, "auth_reason", "forbidden"))
            scope["hq.identity"] = who
            pool_path = path == "/v1/pool" or path.startswith("/v1/pool/")
            if pool_path:
                read = request.method in {"GET", "HEAD"}
                origin = single_header(request, "origin", required=not read)
                fetch_site = single_header(request, "sec-fetch-site", required=False)
                if (origin is not None and origin not in settings.origins) or (fetch_site and fetch_site.lower() == "cross-site"):
                    raise AuthFailure("origin_not_allowed")
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if settings.sample or request.headers.get("x-hq-view") in {"sample", "fixture"}:
                    return await json_error(409, "sample_read_only")(scope, receive, send)
                origin = single_header(request, "origin")
                if origin not in settings.origins or request.headers.get("sec-fetch-site") == "cross-site":
                    raise AuthFailure("origin_not_allowed")
        except AuthFailure as exc:
            request.state.auth_reason = exc.code
            return await json_error(403, exc.code)(scope, receive, send)
        except Exception:
            return await json_error(403, "auth_unavailable")(scope, receive, send)
        await self.app(scope, receive, send)


async def _secret_body(request):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise SecretFailure("json_required", 415)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 400000:
            raise SecretFailure("too_large", 413)
    try:
        from .auth import _json
        value = _json(data)
        if set(value) - {"value", "operation", "request_id"} or "value" not in value:
            raise ValueError()
        return value
    except Exception:
        raise SecretFailure("bad_request") from None


async def list_slots(request):
    store = request.app.state.hq_secrets
    if store is None:
        return json_error(503, "slots_unavailable")
    return JSONResponse({"slots": store.metadata()})


async def store_slot(request):
    store = request.app.state.hq_secrets
    if store is None:
        return json_error(503, "slots_unavailable")
    try:
        data = await _secret_body(request)
        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(store.store, request.path_params["slot"], data["value"],
                                         operation=data.get("operation", "write"), request_id=data.get("request_id"))
        return JSONResponse(result)
    except SecretFailure as exc:
        return json_error(exc.status, exc.code)
    except Exception:
        return json_error(503, "secret_store_failed")


def create_app(*, settings=None, authenticator=None, secret_slots=None, intake=None, sink=None,
               ext_dir=None, run_heartbeat_loop=True, missing_email=None):
    config_error = False
    if settings is None:
        try:
            settings = Settings.load()
        except Exception:
            settings, config_error = Settings(), True
    app = common_app("hq", "0.1.0", sink=sink, run_heartbeat_loop=run_heartbeat_loop,
                     extra_health=lambda: {"config_unavailable": config_error})
    app.state.hq_settings = settings
    app.state.hq_reads = ReadState()
    app.state.hq_intake = intake or IntakeClient()
    app.state.hq_secret_input_needed = set()

    def email_needed():
        app.state.hq_secret_input_needed.add(settings.email_slot)
        if missing_email is None:
            # No guessed ledger/intake payload: the card adapter remains explicit.
            raise NotImplementedError("S-22.7: secret_input card adapter required")
        missing_email(settings.email_slot)

    app.state.hq_auth = authenticator or settings.authenticator(missing_email=email_needed)
    app.state.hq_secrets = secret_slots or settings.secret_slots(emit=sink.emit if sink else None, intake=app.state.hq_intake)
    app.state.hq_sample = settings.sample
    register_api(app)
    # Kept stable for the process, never derived from caller-provided session headers.
    session_key = secrets.token_bytes(32)

    def approval_identity(request):
        from .approval.security import Identity
        who = request.scope.get("hq.identity") or app.state.hq_auth(request)
        if not isinstance(who, dict):
            return None
        session = hmac.new(session_key, who["principal"].encode(), hashlib.sha256).hexdigest()
        return Identity(who["principal"], session, who["direct_tailnet"])

    app.state.hq_approval_identity = approval_identity
    # Import the extension's type only when present; a broken extension stays local.
    try:
        from .ext.b12b2 import BrowserPolicy
        app.state.hq_approval_browser_policy = BrowserPolicy(
            frozenset(urlsplit(origin).netloc for origin in settings.origins), settings.origins)
    except ImportError:
        pass
    app.router.routes.extend([
        Route("/v1/secrets", list_slots, methods=["GET"]),
        Route("/v1/secrets/{slot}", store_slot, methods=["POST"]),
    ])
    app.state.ext_results = load_extensions(app, ext_dir if ext_dir is not None else Path(__file__).parent / "ext")
    # Extensions append routes first, so the root static mount cannot shadow APIs.
    if settings.ui_dir.is_dir():
        app.router.routes.append(Mount("/", app=StaticFiles(directory=settings.ui_dir, html=True), name="hq-ui"))
    app.add_middleware(HQBoundary, owner=app)
    return app
