"""A minimal resource process. Complete control polling before production use."""
from __future__ import annotations

import hmac
import os

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon import creds
from axcommon.http import create_app, json_error, read_json


PLUGIN_ID = "example-resource"
VERSION = "0.1.0"
MODES = {"run", "collect_only", "poll_paused", "draining"}


def build_app():
    state = {"mode": "run", "revision": 0, "knobs": {}}
    apply_token = creds.read_secret_optional("control-apply-token")

    async def status(request: Request) -> JSONResponse:
        return JSONResponse({**state, "running": [], "queued": 0, "last_run": None})

    async def apply(request: Request) -> JSONResponse:
        supplied = creds.parse_bearer(request.headers.get("authorization"))
        if not apply_token or not supplied or not hmac.compare_digest(supplied, apply_token):
            return json_error(401, "unauthorized", "control authorization is required")
        body, error = await read_json(request)
        if error is not None:
            return error
        if not isinstance(body, dict) or body.get("mode") not in MODES or not isinstance(body.get("revision"), int):
            return json_error(422, "bad_request", "mode and revision are required")
        if body["revision"] >= state["revision"]:
            state.update(mode=body["mode"], revision=body["revision"], knobs=dict(body.get("knobs", {})))
        return JSONResponse({"ok": True, "revision": state["revision"]})

    return create_app(
        PLUGIN_ID,
        VERSION,
        routes=[Route("/status", status, methods=["GET"]), Route("/apply", apply, methods=["POST"])],
        run_heartbeat_loop=False,
    )


def main() -> int:
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("AX_HEALTH_PORT", "8080")), log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
