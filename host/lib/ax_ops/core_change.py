"""Prepare core.change instructions for the intake approval flow."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def prepare(spec: str, stdin=None):
    """Read a JSON object from a path or stdin and return destination and payload."""
    stream = stdin if stdin is not None else sys.stdin
    try:
        text = stream.read() if spec == "-" else Path(spec).read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid core.change spec") from exc
    if not isinstance(payload, dict):
        raise ValueError("core.change spec must be an object")
    return "core.change", payload, payload.get("project")
