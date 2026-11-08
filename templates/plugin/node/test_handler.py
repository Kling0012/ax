"""Copy to a bundle test directory after copying handler.py to its final path."""
from __future__ import annotations

import asyncio
import importlib.util


def _load(repo_root):
    path = repo_root / "plugins/nodes/example/echo.py"
    spec = importlib.util.spec_from_file_location("example_echo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_echo_returns_ok(repo_root):
    module = _load(repo_root)

    result = asyncio.run(module.run(None, {"with": {"message": "hello"}}))

    assert result["status"] == "ok"
    assert result["data"] == {"message": "hello"}
