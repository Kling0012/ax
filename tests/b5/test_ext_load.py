"""行き先パッケージと拡張の自動読み込み（差し込み口）の試験。"""
from __future__ import annotations

import textwrap

from core.intake.app import load_dest_packages

from .helpers import client_for, make_app


def test_dest_packages_are_loaded_and_can_register_handlers(fake_transport, tmp_path):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    fake_transport.add("POST", "/v1/mine", json={"mine": True})
    pkg = tmp_path / "dests" / "tpkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(textwrap.dedent("""
        def register(app):
            def handler(app, dest, body, ident):
                return 200, {"mine": True}
            app.state.intake.dest_handlers["demo.mine"] = handler
    """), encoding="utf-8")
    app = make_app(tmp_path, fake_transport, [{
        "name": "demo.mine", "title": "試験", "schema": "healthz", "handler": "入口",
        "allowed_from": ["commander"], "via": ["local"], "tier": "read", "ack": "sync"}],
        dest_dir=tmp_path / "dests")
    assert "demo.mine" in app.state.intake.dest_handlers
    with client_for(app) as c:
        r = c.post("/v1/instructions", json={"from": "commander", "destination": "demo.mine",
                                             "payload": {"ok": True, "component": "d", "version": "1"}})
    assert r.status_code == 200 and r.json()["result"] == {"mine": True}


def test_broken_dest_package_is_isolated(fake_transport, tmp_path):
    good = tmp_path / "dests" / "good"
    good.mkdir(parents=True)
    (good / "__init__.py").write_text(
        "def register(app):\n    app.state.intake.dest_handlers['demo.good'] = lambda *a: (200, {})\n", encoding="utf-8")
    bad = tmp_path / "dests" / "bad"
    bad.mkdir(parents=True)
    (bad / "__init__.py").write_text("raise RuntimeError('壊れている')\n", encoding="utf-8")
    app = make_app(tmp_path, fake_transport, dest_dir=tmp_path / "dests")
    assert app.state.intake.dest_results == {"bad": "RuntimeError", "good": None}
    assert "demo.good" in app.state.intake.dest_handlers
    assert "dest:bad" in app.state.ext_failures  # healthz の ext_failed に出る


def test_real_b5_package_registers_the_two_handlers(fake_transport, tmp_path, repo_root, monkeypatch):
    monkeypatch.setenv("AX_STATE_DIR", str(tmp_path / "state"))
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    app = make_app(tmp_path, fake_transport)  # 既定の dest_dir（core/intake/dest）を読む
    handlers = app.state.intake.dest_handlers
    # b5 が登録する 2 件は必須（契約）。その後は後続の束が増やしうる
    # （b10a: emergency、b12: work.request/core.change/subloop.create、b16b: permission.change）ため、
    # 統合済みの全行き先を固定し、意図しない登録変更を検出する。
    assert {"control.command", "ledger.answer"} <= set(handlers)
    assert set(handlers) == {"control.command", "ledger.answer", "emergency",
                             "permission.change", "work.request", "subloop.create", "core.change",
                             "change.request", "change.cancel_merge", "claim.op", "commitment.update", "decision.record", "goal.update",
                             "ledger.complete", "ledger.park", "life_state.update", "schedule.update",
                             "seat.add", "seat.revoke", "seat.enroll_start", "seat.lock", "seat.tier", "seat.unlock", "secret.push",
                             "work.cancel", "work.stop"}
    assert app.state.intake.dest_results.get("b5") is None


def test_ext_files_are_loaded(fake_transport, tmp_path):
    """core/intake/ext/<pkg>.py の register(app)（axcommon.ext）。"""
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    ext_dir = tmp_path / "ext"
    ext_dir.mkdir()
    (ext_dir / "demo.py").write_text(
        "def register(app):\n    app.state.intake.extra_listener_factories.append('demo')\n", encoding="utf-8")
    app2 = _rebuild_with_ext(tmp_path, fake_transport, ext_dir)
    assert app2.state.ext_results.get("demo") is None
    assert app2.state.intake.extra_listener_factories == ["demo"]


def _rebuild_with_ext(tmp_path, fake_transport, ext_dir):
    from core.intake.app import build_app
    from .helpers import write_registry, simple_dest

    write_registry(tmp_path / "config", [simple_dest()])
    from axcommon.client import TimelineClient
    from axcommon.events import EventSink

    sink = EventSink(TimelineClient("http://timeline:8080", transport=fake_transport), "intake", "test-host",
                     tmp_path / "outbox")
    return build_app(config_dir=tmp_path / "config", transport=fake_transport, sink=sink,
                     dest_dir=tmp_path / "no-dests", ext_dir=ext_dir, run_heartbeat_loop=False)
