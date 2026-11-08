"""axcommon.http: healthz 付きアプリ工場、health.beat、拡張、エラー形式（S-2.1、D-2.2）。starlette が無い環境では飛ばす。"""
import asyncio

import pytest

pytest.importorskip("starlette.testclient")
pytest.importorskip("httpx")

from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from axcommon import schemas  # noqa: E402
from axcommon.events import EventSink  # noqa: E402
from axcommon.http import Heartbeat, create_app, json_error, read_json  # noqa: E402


def _sink(tmp_path, transport=None):
    from axcommon.client import TimelineClient

    return EventSink(TimelineClient("http://timeline:8081", transport=transport) if transport else None, "demo", "hub-host", tmp_path / "ob")


def test_healthz_has_the_s21_shape(tmp_path):
    app = create_app("demo", "1.2.3", sink=_sink(tmp_path))
    with TestClient(app) as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "component": "demo", "version": "1.2.3"}
    assert schemas.errors("healthz", r.json()) == []


def test_extra_health_fields_are_merged_and_failure_of_extra_is_contained(tmp_path):
    app = create_app("demo", "1", sink=_sink(tmp_path), extra_health=lambda: {"projection_skipped": 2})
    with TestClient(app) as c:
        assert c.get("/healthz").json()["projection_skipped"] == 2
    app = create_app("demo", "1", sink=_sink(tmp_path), extra_health=lambda: 1 / 0)
    with TestClient(app) as c:
        r = c.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_beat_is_sent_at_startup(tmp_path, fake_transport):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    app = create_app("demo", "1", sink=_sink(tmp_path, fake_transport), run_heartbeat_loop=False)
    with TestClient(app):
        pass
    evs = [c.json for c in fake_transport.calls]
    assert len(evs) == 1 and evs[0]["kind"] == "health.beat" and evs[0]["source"]["component"] == "demo"
    assert evs[0]["data"]["uptime_s"] >= 0 and "lag_s" in evs[0]["data"]
    assert schemas.errors("event", evs[0]) == []


def test_beat_failure_when_timeline_is_down_does_not_stop_the_app(tmp_path, fake_transport):
    fake_transport.down()
    s = _sink(tmp_path, fake_transport)
    app = create_app("demo", "1", sink=s, run_heartbeat_loop=False)
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
    assert len(s.outbox) == 1  # 溜めた


def test_heartbeat_interval_defaults_to_s17_and_lag_is_the_excess_over_interval(tmp_path, fake_clock):
    t = {"now": 0.0}
    hb = Heartbeat(None, "demo", 60, monotonic=lambda: t["now"])
    assert create_app("demo", "1").state.heartbeat.interval_s == 60  # 既定は S-17 heartbeat_s
    hb.beat()
    t["now"] = 61
    hb.beat()
    t["now"] = 200
    s = EventSink(None, "demo", "h", tmp_path / "ob")
    hb2 = Heartbeat(s, "demo", 60, monotonic=lambda: t["now"])
    t["now"] = 200
    hb2.beat()
    t["now"] = 290
    ev = hb2.beat()
    assert ev["data"]["lag_s"] == 30.0 and ev["data"]["uptime_s"] == 90.0


def test_heartbeat_loop_beats_every_interval_with_stub_sleep(tmp_path):
    s = EventSink(None, "demo", "h", tmp_path / "ob")
    hb = Heartbeat(s, "demo", 60)
    sleeps = []

    async def stub_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(hb.run(sleep=stub_sleep))
    assert sleeps == [60, 60, 60] and hb.beats == 2


def test_unhandled_exception_becomes_500_json_without_internal_detail(tmp_path):
    async def boom(request):
        raise RuntimeError("secret internal path /srv/x")

    app = create_app("demo", "1", routes=[Route("/boom", boom)])
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal"
    assert "secret internal path" not in r.text
    assert schemas.errors("api-error", r.json()) == []


def test_404_and_405_use_error_shape():
    app = create_app("demo", "1")
    with TestClient(app) as c:
        assert c.get("/none").json()["error"]["code"] == "not_found"
        r = c.post("/healthz")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"


def test_read_json_400_for_bad_json_and_422_for_schema_mismatch():
    async def handler(request: Request):
        data, err = await read_json(request, "plugin-apply")
        return err or JSONResponse(data)

    app = create_app("demo", "1", routes=[Route("/apply", handler, methods=["POST"])])
    with TestClient(app) as c:
        assert c.post("/apply", content=b"not json").status_code == 400
        r = c.post("/apply", json={"mode": "run"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "bad_request"
        assert c.post("/apply", json={"mode": "run", "knobs": {}, "revision": 3}).status_code == 200


def test_extensions_register_routes_and_failures_show_in_healthz(tmp_path):
    d = tmp_path / "ext"
    d.mkdir()
    (d / "b9.py").write_text(
        "from starlette.responses import JSONResponse\n"
        "from starlette.routing import Route\n"
        "def register(app):\n"
        "    async def hi(request):\n        return JSONResponse({'hi': 1})\n"
        "    app.router.routes.append(Route('/ext/hi', hi))\n")
    (d / "b7.py").write_text("raise RuntimeError('x')\n")
    app = create_app("demo", "1", ext_dir=str(d))
    with TestClient(app) as c:
        assert c.get("/ext/hi").json() == {"hi": 1}
        h = c.get("/healthz").json()
    assert h["ok"] is True and h["ext_failed"] == ["b7"]


def test_json_error_shape():
    r = json_error(403, "plugin_not_allowed", "no", hint="トークンを確かめる")
    assert r.status_code == 403
    import json

    assert json.loads(r.body) == {"error": {"code": "plugin_not_allowed", "message": "no", "hint": "トークンを確かめる"}}
