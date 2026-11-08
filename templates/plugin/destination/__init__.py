"""Copy to core/intake/dest/example_destination/__init__.py."""
from __future__ import annotations

from .handler import handle


def register(app) -> None:
    app.state.intake.dest_handlers["example.echo"] = handle
