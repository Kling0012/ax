from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import Settings
from .relay import HttpRelay
from .service import Gate


async def _body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length and (not content_length.isdecimal() or int(content_length) > maximum):
        return b"x" * (maximum + 1)
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > maximum:
            break
    return bytes(chunks)


def build_app(gate: Gate):
    async def endpoint(request: Request):
        peer = request.scope.get("client")
        source = peer[0] if peer else ""
        headers = list(request.headers.items())
        body = await _body(request, gate.settings.gate_body_max_kb * 1024)
        reply = await gate.dispatch(request.method, request.url.path + ("?" + request.url.query if request.url.query else ""), headers, source, body)
        if reply.data is None:
            return JSONResponse(None, status_code=reply.status, headers=reply.headers)
        return JSONResponse(reply.data, status_code=reply.status, headers=reply.headers)

    return Starlette(routes=[Route("/healthz", endpoint, methods=["GET"]),
        Route("/mcp", endpoint, methods=["POST"]), Route("/mcp/", endpoint, methods=["POST"]),
        Route("/v1/{rest:path}", endpoint, methods=["GET", "POST"])])



def build_from_environment():
    """Build the gate ASGI application with its fixed intake:8082 relay."""
    settings = Settings.from_env()
    return build_app(Gate(settings, transport=HttpRelay(settings.intake_host, settings.intake_port)))
