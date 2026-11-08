"""A timer-runner handler. Copy to plugins/timer/example-timer/handler.py."""
from __future__ import annotations


async def run(ctx):
    """Record a local observation. Replace this with an idempotent timer action."""
    result = {"state": "completed"}
    ctx.emit("automation.observed", "example-timer", result)
    return result
