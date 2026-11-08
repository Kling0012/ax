"""Serve the independent T-52 interface through the HQ boundary."""
from pathlib import Path

from starlette.responses import FileResponse, RedirectResponse
from starlette.routing import Route

from core.hq.api import authorize

_ROOT = Path(__file__).resolve().parent.parent / "b10ui"
_FILES = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}


def register(app):
    async def index(request):
        denied = await authorize(request)
        if denied is not None:
            return denied
        return RedirectResponse("/b10/", status_code=307)

    async def asset(request):
        denied = await authorize(request)
        if denied is not None:
            return denied
        name = request.path_params.get("name", "index.html")
        if name not in _FILES:
            from starlette.responses import PlainTextResponse
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(_ROOT / name, media_type=_FILES[name], headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        })

    app.router.routes.extend([Route("/b10", index), Route("/b10/", asset), Route("/b10/{name}", asset)])
