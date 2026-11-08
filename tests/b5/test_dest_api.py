"""GET /v1/destinations（S-6.3、S-6.15 の destinations の部分）の試験。"""
from __future__ import annotations

from .helpers import HQ, LOCAL, client_for, make_app, simple_dest


def test_lists_destinations_with_tier_and_mcp_tool(fake_transport, tmp_path):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    entries = [
        simple_dest("demo.echo"),
        dict(simple_dest("demo.control", tier="operate", mcp_tool="ax_control")),
    ]
    app = make_app(tmp_path, fake_transport, entries)
    with client_for(app) as c:
        r = c.get("/v1/destinations")
    assert r.status_code == 200
    body = r.json()
    assert [d["name"] for d in body["destinations"]] == ["demo.control", "demo.echo"]  # 名前順
    by_name = {d["name"]: d for d in body["destinations"]}
    demo = by_name["demo.control"]
    assert demo["tier"] == "operate" and demo["mcp_tool"] == "ax_control" and demo["schema"] == "healthz"
    assert "mcp_tool" not in by_name["demo.echo"]
    assert body["reads"] == []  # reads は b12b3 の担当


def test_hides_destinations_above_tier_and_out_of_via(fake_transport, tmp_path):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    entries = [
        simple_dest("demo.irr", tier="irreversible", irreversible=True),
        simple_dest("demo.hqonly", via=["hq"]),
        simple_dest("demo.visible"),
    ]
    app = make_app(tmp_path, fake_transport, entries)
    with client_for(app) as c:  # local の段は operate まで
        body = c.get("/v1/destinations").json()
    assert [d["name"] for d in body["destinations"]] == ["demo.visible"]
    with client_for(app, HQ) as c:  # hq は全部の段
        body = c.get("/v1/destinations").json()
    assert [d["name"] for d in body["destinations"]] == ["demo.hqonly", "demo.irr", "demo.visible"]


def test_requires_ident(fake_transport, tmp_path):
    """受け口の資格が無い（どの受け口でもない）と、行き先の一覧も出さない。"""
    from starlette.testclient import TestClient

    fake_transport.add("POST", "/v1/events", json={"ok": True})
    app = make_app(tmp_path, fake_transport)
    with TestClient(app) as c:
        r = c.get("/v1/destinations")
        assert r.status_code == 403
        r = c.post("/v1/instructions", json={"from": "commander", "destination": "demo.echo", "payload": {}})
        assert r.status_code == 403 and r.json()["error"]["code"] == "auth.failed"
