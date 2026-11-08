"""Copy to core/intake/dest/example_destination/handler.py."""
from __future__ import annotations


def handle(app, dest, body, ident):
    """Return an in-process result after intake has validated the payload."""
    return 200, {"message": body["payload"]["message"]}
