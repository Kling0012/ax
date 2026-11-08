"""tests/b9 の共通 fixture。ネットワークとサービスを使わない。

- world        本物の timeline・control・ledger を FaultNet に載せた組立（tests/fakes/b9/world.py）。プラグインは pa、pb、pc。
- sync_clock   FakeClock を timeutil に入れる fake_clock（tests/conftest.py）をそのまま使う。
starlette・httpx・jsonschema が無い環境では、これを使う試験だけが飛ぶ。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def world(tmp_path, repo_root, fake_clock, monkeypatch):
    pytest.importorskip("jsonschema")
    pytest.importorskip("starlette.testclient")
    pytest.importorskip("httpx")
    from tests.fakes.b9.world import World
    from tests.fakes.b7 import control

    control.reset()
    w = World(tmp_path, repo_root, fake_clock, monkeypatch, plugin_ids=("pa", "pb", "pc"))
    yield w
    for obj in (w.control, w.ledger, w.resources_service):
        try:
            obj.store.close()
        except Exception:  # noqa: BLE001
            pass
