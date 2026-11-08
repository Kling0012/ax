"""S-6.5 の検証と S-6.2 の資格の照合の試験（T-6 の骨格の部分）。

Schema 違反の確認には既存の schemas/healthz（ok/component/version が要る）を payload の Schema に使う。
"""
from __future__ import annotations

import pytest

from .helpers import LOCAL, HQ, client_for, events_of, instruction, make_app, simple_dest

VALID = {"ok": True, "component": "demo", "version": "1"}


def _app(fake_transport, tmp_path, entries=None):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    fake_transport.add("POST", "/v1/echo", json={"echoed": True})
    return make_app(tmp_path, fake_transport, entries if entries is not None else
                    [simple_dest(tier="operate", schema="healthz", conflict_key="card:{todo_id}")])


def test_unknown_destination_is_rejected(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path)
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("no.such"))
        assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_destination"
    assert events_of(fake_transport, "instruction.rejected")[0]["data"]["reason"] == "unknown_destination"


def test_body_from_mismatch_is_forged_authority(fake_transport, tmp_path):
    """内部コンテナが from=hq を名乗っても、資格（commander）と違えば拒否する（T-6）。"""
    app = _app(fake_transport, tmp_path)
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("demo.echo", VALID, **{"from": "hq"}))
        assert r.status_code == 403 and r.json()["error"]["code"] == "forged_authority"
    ev = events_of(fake_transport, "instruction.rejected")
    assert ev and ev[0]["data"]["reason"] == "forged_authority"


def test_body_principal_and_via_are_not_authority(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path)
    with client_for(app) as c:
        # principal / via は本文に書いても無視される（schema の additionalProperties: false で 422）
        r = c.post("/v1/instructions", json={"from": "commander", "destination": "demo.echo",
                                             "payload": VALID, "principal": "user", "via": "hq"})
        assert r.status_code == 422


def test_allowed_from_violation_is_rejected(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path, [simple_dest(tier="operate", allowed_from=["hq"])])
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("demo.echo", VALID))
        assert r.status_code == 403 and r.json()["error"]["code"] == "not_allowed_from"


def test_via_violation_is_receptacle_not_allowed(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path, [simple_dest(tier="operate", via=["hq"])])
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("demo.echo", VALID))
        assert r.status_code == 403 and r.json()["error"]["code"] == "receptacle_not_allowed"


def test_tier_insufficient_is_rejected(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path, [simple_dest(tier="irreversible", irreversible=True)])
    with client_for(app) as c:  # local の段は operate まで（S-6.8）
        r = c.post("/v1/instructions", json=instruction("demo.echo", VALID, idempotency_key="k1"))
        assert r.status_code == 403 and r.json()["error"]["code"] == "tier_insufficient"


def test_confirm_irreversible_from_commander_is_forged(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path, [simple_dest(tier="irreversible", irreversible=True)])
    with client_for(app) as c:
        r = c.post("/v1/instructions",
                   json=instruction("demo.echo", VALID, idempotency_key="k1", confirm_irreversible=True))
        assert r.status_code == 403 and r.json()["error"]["code"] == "forged_authority"


def test_irreversible_tier_request_becomes_approval_wait(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path, [simple_dest(tier="irreversible", irreversible=True)])
    with client_for(app, HQ) as c:
        r = c.post("/v1/instructions",
                   json=instruction("demo.echo", VALID, **{"from": "hq", "idempotency_key": "k1"}))
        assert r.status_code == 202 and r.json()["reason"] == "approval_required"


def test_payload_schema_violation_is_rejected(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path)
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("demo.echo", {"ok": True}))
        assert r.status_code == 422 and r.json()["error"]["code"] == "payload_schema"
        r = c.post("/v1/instructions", json=instruction("demo.echo", {**VALID, "issued_by": "manager"}))
        assert r.status_code == 403 and r.json()["error"]["code"] == "forged_authority"


def test_expired_instruction_is_rejected(fake_transport, tmp_path, fake_clock):
    app = _app(fake_transport, tmp_path)
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("demo.echo", VALID, idempotency_key="k",
                                                        expires_at="2026-11-05T11:00:00.000Z"))
        assert r.status_code == 422 and r.json()["error"]["code"] == "expired"


def test_operate_requires_idempotency_key(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path)
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("demo.echo", VALID))
        assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_required"


def test_read_tier_destination_does_not_need_key(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path, [simple_dest(tier="read")])
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("demo.echo", VALID))
        assert r.status_code == 200


def test_bad_instruction_shape_is_rejected(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path)
    with client_for(app) as c:
        r = c.post("/v1/instructions", json={"destination": "demo.echo"})  # payload が無い
        assert r.status_code == 422 and r.json()["error"]["code"] == "bad_instruction"
        r = c.post("/v1/instructions", content=b"not json", headers={"Content-Type": "application/json"})
        assert r.status_code == 400


def test_same_idempotency_key_is_processed_once(fake_transport, tmp_path):
    """T-6 / S-6.16: 同じ idempotency_key は1回だけ処理する。"""
    app = _app(fake_transport, tmp_path)
    body = instruction("demo.echo", VALID, idempotency_key="once-1")
    with client_for(app) as c:
        r1 = c.post("/v1/instructions", json=body)
        r2 = c.post("/v1/instructions", json=body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["replayed"] is False and r2.json()["replayed"] is True
    assert r2.json()["result"] == r1.json()["result"]
    assert len([p for p in fake_transport.paths("POST") if p == "/v1/echo"]) == 1  # 1 回だけ渡す


def test_same_key_different_payload_is_conflict(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path)
    with client_for(app) as c:
        c.post("/v1/instructions", json=instruction("demo.echo", VALID, idempotency_key="k"))
        r = c.post("/v1/instructions", json=instruction("demo.echo", {**VALID, "version": "2"}, idempotency_key="k"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_mismatch"


def test_events_carry_verified_principal_and_via(fake_transport, tmp_path):
    app = _app(fake_transport, tmp_path)
    with client_for(app) as c:
        c.post("/v1/instructions", json=instruction("demo.echo", VALID, idempotency_key="k", session="s-1"))
    got = events_of(fake_transport)
    by_kind = {e["kind"]: e for e in got}
    assert {"instruction.received", "instruction.routed"} <= set(by_kind)
    for kind in ("instruction.received", "instruction.routed"):
        src = by_kind[kind]["source"]
        assert src["component"] == "intake" and src["principal"] == "local-operator"
        assert src["via"] == "local" and src["session"] == "s-1"


def test_response_envelope_matches_schema(fake_transport, tmp_path, repo_root):
    from axcommon import schemas

    app = _app(fake_transport, tmp_path)
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("demo.echo", VALID, idempotency_key="k"))
    assert schemas.errors("ext/b5/instruction-response", r.json()) == []


def test_handler_down_is_unavailable(fake_transport, tmp_path):
    from tests.fakes.http import FakeTransport

    events_transport = FakeTransport()
    events_transport.add("POST", "/v1/events", json={"ok": True})
    fake_transport.down()
    app = make_app(tmp_path, fake_transport,
                   [simple_dest(tier="operate", schema="healthz", conflict_key="card:{todo_id}")],
                   sink_transport=events_transport)
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("demo.echo", VALID, idempotency_key="k"))
    assert r.status_code == 503 and r.json()["error"]["code"] == "handler_unavailable"
    rejects = events_of(events_transport, "instruction.rejected")
    assert rejects and rejects[0]["data"]["reason"] == "handler_unavailable"
    fake_transport.up()
