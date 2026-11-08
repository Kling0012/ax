"""全束の試験の共通 fixture（B0）。ネットワークとサービスを使わない。

- repo_root          リポジトリの根（Path）
- ci_env             tools/ci-fixtures/ci.env を dict で返す
- fake_clock         tests/fakes/clock.py の FakeClock（install 済み。終了時に戻す）
- fake_transport     tests/fakes/http.py の FakeTransport
- fake_gateway       tests/fakes/model_gateway.py の FakeModelGateway（fake_clock つき）
- 自動: 試験中の socket.connect を拒否する（外部に触れる試験を書けないようにする。docs/test-procedure.md）
一時ファイルは pytest の tmp_path（環境変数 TMPDIR の下）を使う。
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_configure(config):
    config.addinivalue_line("markers", "realenv: 実環境でしか通せない試験。CI では実行しない")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _deny(self, *a, **k):
        raise RuntimeError("試験中のネットワーク接続は禁止（偽の部品 tests/fakes/ を使う）")

    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny)
    yield


@pytest.fixture(autouse=True)
def _repo_dirs(monkeypatch):
    monkeypatch.setenv("AX_SCHEMAS_DIR", str(ROOT / "schemas"))
    monkeypatch.setenv("AX_CONFIG_DIR", str(ROOT / "config"))
    from axcommon import schemas

    schemas.clear_cache()
    yield


@pytest.fixture
def repo_root() -> Path:
    return ROOT


@pytest.fixture
def ci_env(repo_root) -> dict:
    out = {}
    for line in (repo_root / "tools" / "ci-fixtures" / "ci.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


@pytest.fixture
def fake_clock():
    from tests.fakes.clock import FakeClock

    c = FakeClock().install()
    yield c
    c.uninstall()


@pytest.fixture
def fake_transport():
    from tests.fakes.http import FakeTransport

    return FakeTransport()


@pytest.fixture
def fake_gateway(fake_clock):
    from tests.fakes.model_gateway import FakeModelGateway

    return FakeModelGateway(clock=fake_clock)
