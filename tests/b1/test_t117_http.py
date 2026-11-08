"""T-117: 投影の HTTP の部分。starlette と httpx が無い環境では、このファイルだけを飛ばす。"""
import pytest

pytest.importorskip("starlette.testclient")
pytest.importorskip("httpx")

from core.timeline import projectors
from tests.fakes.b1.events import full_sequence, make_event

# --- HTTP ------------------------------------------------------------------------------------
tc = pytest.importorskip("starlette.testclient")
pytest.importorskip("httpx")


def _client(tmp_path):
    from axcommon.redact import Redactor
    from core.timeline import app as tl_app
    cfg = tl_app.Config(host="hub-host", db=str(tmp_path / "h.sqlite"), archive_dir=str(tmp_path / "a"), redact_index=None, pii_list=None, export_interval_s=0)
    core, _work, svc = tl_app.build(cfg, redactor=Redactor(), run_heartbeat_loop=False)
    return tc.TestClient(core), svc


def test_http_projection_names_and_404(tmp_path):
    c, svc = _client(tmp_path)
    with c:
        svc.store.append(full_sequence())
        for n in projectors.PROJECTION_NAMES:
            r = c.get(f"/v1/projections/{n}")
            assert r.status_code == 200 and r.json()["name"] == n
        for bad in ("todos", "events", "Runs", "plugin"):
            assert c.get(f"/v1/projections/{bad}").status_code == 404


def test_http_projection_response_matches_schema_and_filters(tmp_path):
    from axcommon import schemas
    c, svc = _client(tmp_path)
    with c:
        svc.store.append(full_sequence())
        body = c.get("/v1/projections/runs").json()
        assert schemas.errors("projection-response", body) == []
        assert body["upto_seq"] == svc.store.max_seq() > 0
        act = c.get("/v1/projections/runs", params={"active": "true"}).json()["rows"]
        assert {r["key"] for r in act} == {"RUN01", "RUN02", "RUN03"}
        assert [r["key"] for r in c.get("/v1/projections/jobs", params={"key": "J-stalled"}).json()["rows"]] == ["J-stalled"]
        assert c.get("/v1/projections/plugins", params={"active": "true"}).status_code == 422
        assert c.get("/v1/projections/health", params={"active": "true"}).status_code == 422
        assert c.get("/v1/projections/runs", params={"active": "maybe"}).status_code == 422
        assert c.get("/v1/projections/runs", params={"limit": 5000}).status_code == 422
        ctl = c.get("/v1/projections/controls", params={"active": "true"}).json()["rows"]
        assert [r["key"] for r in ctl] == ["CMD1"]


def test_http_skip_increments_projection_skipped_in_healthz(tmp_path):
    c, _svc = _client(tmp_path)
    with c:
        base = c.get("/healthz").json()["projection_skipped"]
        r = c.post("/v1/events", json=[make_event("run.state", "R1", {}),                      # data.state が無い
                                       make_event("job.state", "pool", {"state": "running"}),   # data.job_id が無い
                                       make_event("control.issued", "C", {})])                  # data.command が無い
        assert r.status_code == 200 and r.json()["accepted"] == 3                              # 追記は成功
        assert c.get("/healthz").json()["projection_skipped"] == base + 3
        for n in ("runs", "jobs", "controls"):
            assert c.get(f"/v1/projections/{n}").json()["rows"] == []


def test_http_export_api(tmp_path):
    from tests.b1.helpers import ext_errors
    c, svc = _client(tmp_path)
    with c:
        svc.store.append(full_sequence())
        top = svc.store.max_seq()
        r = c.post("/v1/export", json={"to_seq": top})                       # seq の 1 から top まで
        assert r.status_code == 200 and ext_errors("export-response", r.json()) == []
        assert r.json()["exported"] == top and r.json()["last_seq"] == top
        assert c.post("/v1/export", json={"to_seq": top + 10}).json()["exported"] == 0        # 続きは無い
        assert c.post("/v1/export", json={"bogus": 1}).status_code == 422
        assert c.post("/v1/export", json={"days": -1}).status_code == 422
        assert c.post("/v1/export", json={"days": 100000}).json()["exported"] == 0      # 範囲に合う事象が無い
        assert c.post("/v1/export", json={"before_ts": "yesterday"}).status_code == 422
        assert c.post("/v1/export", json={"to_seq": 0}).status_code == 422
        assert c.post("/v1/export", content=b"{bad").status_code == 400
        assert c.post("/v1/export").status_code == 200                      # 本文なし: timeline_hot_days より古い区間（今回は無い）
        assert c.delete("/v1/export").status_code == 405


def test_http_export_is_not_on_the_work_port(tmp_path):
    from axcommon.redact import Redactor
    from core.timeline import app as tl_app
    cfg = tl_app.Config(host="hub-host", db=str(tmp_path / "w.sqlite"), archive_dir=str(tmp_path / "a"), redact_index=None, pii_list=None, export_interval_s=0)
    _core, work, _svc = tl_app.build(cfg, redactor=Redactor(), run_heartbeat_loop=False)
    with tc.TestClient(work) as w:
        assert w.post("/v1/export", json={}).status_code in (404, 405)
