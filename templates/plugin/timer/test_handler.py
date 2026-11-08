"""Copy to a bundle test directory after placing the timer at its final path."""
from __future__ import annotations

import asyncio
import importlib.util

import yaml

from axcommon import schemas


class FakeContext:
    def __init__(self):
        self.events = []

    def emit(self, kind, subject, data):
        self.events.append((kind, subject, data))


def _load(repo_root):
    path = repo_root / "plugins/timer/example-timer/handler.py"
    spec = importlib.util.spec_from_file_location("example_timer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_timer_manifest_matches_schema(repo_root):
    path = repo_root / "plugins/timer/example-timer/manifest.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert schemas.errors("manifest", manifest) == []


def test_example_timer_records_one_observation(repo_root):
    module = _load(repo_root)
    context = FakeContext()

    result = asyncio.run(module.run(context))

    assert result == {"state": "completed"}
    assert context.events == [("automation.observed", "example-timer", result)]
