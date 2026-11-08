"""Copy to plugins/nodes/example/echo.py and replace the example values."""
from __future__ import annotations

from runtime.subloop.nodeapi import ok


TYPE = "example.echo"
schema = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "minLength": 1, "maxLength": 200},
    },
    "required": ["message"],
    "additionalProperties": False,
}
META = {"statuses": ["ok"]}


async def run(ctx, inp):
    """Return a validated value without performing an external operation."""
    return ok({"message": inp["with"]["message"]})
