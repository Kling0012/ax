"""T-1: 時系列記録の HTTP の部分（8080 と 8081）。starlette と httpx が無い環境では、このファイルだけを飛ばす。"""
import hashlib
import threading
import time

import pytest

pytest.importorskip("starlette.testclient")
pytest.importorskip("httpx")

from axcommon import creds
from axcommon.redact import REDACTED, Redactor
from core.timeline import app as tl_app, kinds
from tests.b1.helpers import ext_errors
from tests.fakes.b1.events import make_event

# --- HTTP（starlette がある環境） -------------------------------------------------------------
pytest.importorskip("starlette.testclient")
pytest.importorskip("httpx")
from starlette.testclient import TestClient  # noqa: E402


def _sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


@pytest.fixture
def app_parts(tmp_path):
    tokens = creds.TokenTable({"sl-demo": _sha("tok-demo"), "hibi-state": _sha("tok-hibi")})
    cfg = tl_app.Config(host="hub-host", db=str(tmp_path / "t.sqlite"), archive_dir=str(tmp_path / "archive"), redact_index=None, pii_list=None,
                        export_interval_s=0, hot_days=30, longpoll_s=5)
    core, work, svc = tl_app.build(cfg, redactor=Redactor(), tokens=tokens, run_heartbeat_loop=False)
    return core, work, svc


def _post(c, ev, token=None):
    h = {"Authorization": "Bearer " + token} if token else {}
    return c.post("/v1/events", json=ev, headers=h)


def test_http_post_get_and_shape(app_parts):
    core, _work, _svc = app_parts
    with TestClient(core) as c:
        base = c.get("/v1/events").json()["next_after"]                     # 起動時の health.beat の分
        e = make_event("run.state", "R1", {"state": "running"})
        r = _post(c, e)
        assert r.status_code == 200 and r.json()["accepted"] == 1 and ext_errors("events-post-response", r.json()) == []
        assert _post(c, e).json() == {"accepted": 0, "duplicates": 1, "events": [{"id": e["id"], "seq": base + 1, "duplicate": True}]}
        lst = c.get("/v1/events", params={"after": base}).json()
        assert ext_errors("events-list-response", lst) == [] and lst["next_after"] == base + 1
        assert lst["events"][0]["id"] == e["id"] and lst["events"][0]["seq"] == base + 1
        assert c.get(f"/v1/events/{e['id']}").json()["subject"] == "R1"
        assert c.get("/v1/events/nope").status_code == 404
        assert _post(c, [make_event("run.state", f"B{i}", {"state": "running"}) for i in range(3)]).json()["accepted"] == 3
        assert len(c.get("/v1/events", params={"after": base, "limit": 2}).json()["events"]) == 2
        assert c.get("/v1/events", params={"after": base, "kind": "run.state,job.state", "subject": "R1"}).json()["events"][0]["subject"] == "R1"


def test_http_rejects_bad_events_and_bad_params(app_parts):
    core, _w, _s = app_parts
    with TestClient(core) as c:
        assert _post(c, {"id": "x"}).status_code == 422
        assert _post(c, []).status_code == 422
        assert _post(c, dict(make_event("run.state", "R1", {"state": "running"}), ts="yesterday")).status_code == 422
        assert _post(c, dict(make_event("run.state", "R1", {"state": "running"}), extra=1)).status_code == 422
        assert _post(c, dict(make_event("run.state", "R1", {"state": "running"}), kind="NoDot")).status_code == 422
        # data の中身では断らない（S-3.6: 投影の必須の項目が無くても追記は成功し、投影だけ飛ばす）
        assert _post(c, make_event("run.state", "R2", {"state": "bogus"})).status_code == 200
        assert c.get("/v1/events", params={"after": "x"}).status_code == 422
        assert c.get("/v1/events", params={"limit": 0}).status_code == 422
        assert c.post("/v1/events", content=b"{not json").status_code == 400


def test_http_has_no_update_or_delete_routes(app_parts):
    core, work, _s = app_parts
    for app in (core, work):
        for r in app.routes:
            methods = set(getattr(r, "methods", None) or [])
            assert not (methods & {"PUT", "PATCH", "DELETE"}), (r.path, methods)
    with TestClient(core) as c:
        e = make_event("run.state", "R1", {"state": "running"})
        _post(c, e)
        for m in ("put", "patch", "delete"):
            assert getattr(c, m)(f"/v1/events/{e['id']}").status_code == 405
            assert getattr(c, m)("/v1/events").status_code == 405
        assert c.get(f"/v1/events/{e['id']}").status_code == 200


def test_http_secret_in_post_is_replaced(app_parts):
    core, _w, _s = app_parts
    token = "gh" + "p_" + "Zy9" * 12
    with TestClient(core) as c:
        e = make_event("error.classified", "x", {"class": "unknown", "code": "unclassified", "message": "t " + token + " u"})
        _post(c, e)
        got = c.get(f"/v1/events/{e['id']}").json()
        assert token not in str(got) and REDACTED in got["data"]["message"]


def test_longpoll_returns_when_a_new_event_arrives(app_parts):
    core, _w, _s = app_parts
    with TestClient(core) as c:
        base = c.get("/v1/events").json()["next_after"]
        out = {}

        def waiter():
            t0 = time.monotonic()
            out["r"] = c.get("/v1/events", params={"after": base, "wait": 4}).json()
            out["dt"] = time.monotonic() - t0

        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.3)
        e = make_event("run.state", "LP", {"state": "running"})
        _post(c, e)
        th.join(timeout=6)
        assert out["r"]["events"][0]["id"] == e["id"] and out["dt"] < 3.5


def test_longpoll_times_out_empty_and_is_capped(app_parts):
    core, _w, svc = app_parts
    svc.longpoll_s = 0.3
    with TestClient(core) as c:
        base = c.get("/v1/events").json()["next_after"]
        t0 = time.monotonic()
        r = c.get("/v1/events", params={"after": base, "wait": 60}).json()          # wait は longpoll_s で打ち切る
        assert r == {"events": [], "next_after": base} and time.monotonic() - t0 < 3


# --- 作業側の口 8081 --------------------------------------------------------------------------
def test_work_port_needs_a_known_token(app_parts):
    _c, work, _s = app_parts
    with TestClient(work) as w:
        e = make_event("run.state", "R1", {"state": "running"})
        assert _post(w, e).status_code == 401
        assert _post(w, e, "wrong").status_code == 401
        assert _post(w, e, "tok-demo").status_code == 200
        assert w.get("/v1/events").status_code == 401
        assert w.get("/v1/events", headers={"Authorization": "Bearer tok-demo"}).status_code == 200


def test_work_port_without_token_list_is_closed(tmp_path):
    cfg = tl_app.Config(host="hub-host", db=str(tmp_path / "t.sqlite"), archive_dir=str(tmp_path / "a"), redact_index=None, pii_list=None, export_interval_s=0)
    _core, work, _svc = tl_app.build(cfg, redactor=Redactor(), tokens=None, run_heartbeat_loop=False)
    with TestClient(work) as w:
        assert _post(w, make_event("run.state", "R1", {"state": "running"}), "anything").status_code == 401
        assert w.get("/healthz").json()["plugin_tokens"] is False


def test_work_port_pins_component_and_kinds(app_parts):
    core, work, _s = app_parts
    with TestClient(core) as cc, TestClient(work) as w:
        # component を偽る -> 拒否
        r = _post(w, make_event("run.state", "R1", {"state": "running"}, component="ledger"), "tok-demo")
        assert r.status_code == 403 and r.json()["error"]["code"] == "component_mismatch"
        # 書き手が別の kind -> 拒否
        r = _post(w, make_event("control.issued", "C", {"command": {"id": "C"}}, component="sl-demo"), "tok-demo")
        assert r.status_code == 403 and r.json()["error"]["code"] == "kind_not_allowed"
        # 入口が確かめる値は本文から受けない
        r = _post(w, make_event("run.state", "R1", {"state": "running"}, source_extra={"principal": "user"}), "tok-demo")
        assert r.status_code == 403 and r.json()["error"]["code"] == "reserved_field"
        # サブループの kind は通る
        assert _post(w, make_event("run.state", "R1", {"state": "running"}), "tok-demo").status_code == 200
        # 拒否したことは events.rejected に残る（偽られた本文は残らない）
        rej = cc.get("/v1/events", params={"kind": "events.rejected"}).json()["events"]
        assert {e["data"]["reason"] for e in rej} == {"component_mismatch", "kind_not_allowed", "reserved_field"}
        assert all(e["source"]["component"] == "timeline" and e["subject"] == "sl-demo" for e in rej)
        # 拒否された事象そのものは書かれていない
        assert not cc.get("/v1/events", params={"kind": "control.issued"}).json()["events"]


def test_work_port_manifest_declared_kinds_are_allowed(tmp_path):
    cfg = tl_app.Config(host="hub-host", db=str(tmp_path / "t.sqlite"), archive_dir=str(tmp_path / "a"), redact_index=None, pii_list=None, export_interval_s=0)
    tokens = creds.TokenTable({"sl-demo": _sha("tok-demo")})
    _core, work, _svc = tl_app.build(cfg, redactor=Redactor(), tokens=tokens, run_heartbeat_loop=False,
                                    manifest_kinds=lambda pid: {"inspection.phase"} if pid == "sl-demo" else set())
    with TestClient(work) as w:
        e = make_event("inspection.phase", "I1", {"phase": "p1"})
        assert _post(w, e, "tok-demo").status_code == 200


def test_work_port_exposes_only_events_api(app_parts):
    core, work, _s = app_parts
    paths = {r.path for r in work.routes}
    assert paths == {"/healthz", "/v1/events", "/v1/events/{event_id}"}
    with TestClient(work) as w:
        h = {"Authorization": "Bearer tok-demo"}
        for p in ("/v1/projections/runs", "/v1/export", "/v1/commands", "/v1/todos"):
            assert w.get(p, headers=h).status_code in (404, 405), p
        assert w.post("/v1/export", json={}, headers=h).status_code in (404, 405)
    with TestClient(core) as c:
        assert c.get("/v1/projections/runs").status_code == 200


def test_healthz_shape_and_extras(app_parts):
    core, _w, _s = app_parts
    with TestClient(core) as c:
        h = c.get("/healthz").json()
    assert h["ok"] is True and h["component"] == "timeline" and "projection_skipped" in h and "last_seq" in h


def test_own_beat_is_recorded_at_startup(app_parts):
    core, _w, _s = app_parts
    with TestClient(core) as c:
        beats = c.get("/v1/events", params={"kind": "health.beat"}).json()["events"]
    assert beats and beats[0]["source"]["component"] == "timeline"
    assert beats[0]["data"]["uptime_s"] >= 0


def test_work_side_kinds_cover_every_subloop_and_component_writer_in_s33():
    # S-3.3 の書き手がサブループ・各部品の kind は、作業側の口で通る
    for k in ("run.state", "item.state", "node.state", "health.beat", "error.classified", "resource.repeat_failure"):
        assert kinds.kind_allowed("sl-x", k)
    for k in ("control.issued", "task.state", "plugin.registered", "job.state", "archive.moved", "memory.distilled", "emergency.stop"):
        assert not kinds.kind_allowed("sl-x", k)
