"""B12 intake destinations; each optional sibling fails locally."""
from __future__ import annotations

import importlib

from core.intake.dest.b12 import common, core_change, work


def register(app):
    common.register(app)
    work.register(app)
    core_change.register(app)
    for name in ("subloop", "read_relay"):
        module_name = "core.intake.dest.b12." + name
        try:
            module = importlib.import_module(module_name)
            module.register(app)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                app.state.ext_failures.append("b12:" + name)
        except Exception:
            app.state.ext_failures.append("b12:" + name)
    # An absent subloop package must never fall back to generic POST forwarding.
    app.state.intake.dest_handlers.setdefault("subloop.create", unavailable_subloop)


def unavailable_subloop(app, dest, body, ident):
    return 501, {"error": {"code": "subloop_handler_unavailable", "message": "subloop_handler_unavailable"}}
