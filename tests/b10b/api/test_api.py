"""T-50/T-56/T-57/T-59: HQ API contracts with fake ledger and intake."""
from __future__ import annotations

from datetime import timedelta

from starlette.testclient import TestClient

from axcommon.redact import Redactor
from axcommon.http import create_app
from core.hq.api import make_routes, register
from core.hq.api.service import BusinessAPI
from tests.fakes.b10b.api.parts import FakeDefaults, FakeIntake, FakeLedger, fixed_now


class FakeRegistry:
    def maybe_reload(self):
        return False

    def get(self, destination):
        handler = "not-implemented:work.cancel" if destination == "work.cancel" else "http://fake.invalid/handler"
        return {"handler": handler}


def fresh(ts=0):
    return (fixed_now() - timedelta(seconds=ts)).isoformat().replace("+00:00", "Z")


def client(ledger=None, intake=None, *, direct=True, sample=False):
    ledger = ledger or FakeLedger()
    app = create_app("test", "0", run_heartbeat_loop=False)
    app.state.hq_auth = lambda request: {"allowed": True, "via": "tailnet" if direct else "public", "direct_tailnet": direct}
    app.state.hq_sample = sample
    app.state.hq_api = BusinessAPI(ledger, intake=intake or FakeIntake(), defaults=FakeDefaults(), now=fixed_now,
                                   redactor=Redactor(), registry=FakeRegistry())
    app.router.routes.extend(make_routes(app.state.hq_api))
    return TestClient(app), ledger, app.state.hq_api


def test_t50_request_receipt_and_duplicate_do_not_submit_twice():
    intake = FakeIntake()
    c, _, _ = client(intake=intake)
    body = {"op_id": "one", "payload": {"project": "ax", "title": "read", "role": "summary"}}
    first = c.post("/v1/requests", json=body)
    second = c.post("/v1/requests", json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["todo_ref"] == "#/todo/todo-1"
    assert len(intake.calls) == 2
    assert intake.calls[0]["idempotency_key"] == intake.calls[1]["idempotency_key"]


def test_t50_preserves_input_when_intake_fails():
    intake = FakeIntake()
    intake.down = True
    c, _, _ = client(intake=intake)
    response = c.post("/v1/requests", json={"op_id": "one", "payload": {"project": "ax", "title": "read", "role": "summary"}})
    assert response.status_code == 503
    assert response.json()["preserve_input"] is True


def test_mutation_is_rejected_server_side_for_a_sample_view():
    intake = FakeIntake()
    c, _, _ = client(intake=intake, sample=True)
    response = c.post("/v1/requests", json={"op_id": "one", "payload": {"project": "ax", "title": "read", "role": "summary"}})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "sample_read_only"
    assert intake.calls == []


def test_register_reuses_hq_authenticator_and_prebuilt_api():
    ledger = FakeLedger({"/v1/focus-state": {"state": "focus", "since": fresh(20), "as_of": fresh(10)}})
    app = create_app("test", "0", run_heartbeat_loop=False)
    app.state.hq_auth = lambda request: None
    app.state.hq_api = BusinessAPI(ledger, intake=FakeIntake(), defaults=FakeDefaults(), now=fixed_now, redactor=Redactor())
    register(app)
    c = TestClient(app)
    assert c.get("/v1/focus-state").status_code == 403
    app.state.hq_auth = lambda request: {"allowed": True, "via": "tailnet", "direct_tailnet": True}
    assert c.get("/v1/focus-state").status_code == 200


def test_t56_history_due_unfinished_and_digest_failure_are_explicit():
    ledger = FakeLedger({
        "/v1/promises": {"promises": [{"id": "promise-1", "todo_id": "todo-1", "due_at": "2026-11-08", "state": "open"}]},
        "/v1/promises/promise-1": {"id": "promise-1", "history": [{"version": 1}]},
        "/v1/todos/todo-1": {"id": "todo-1", "state": "running"},
    })
    c, _, _ = client(ledger)
    listed = c.get("/v1/promises", params={"state": "open", "due": "1"})
    item = c.get("/v1/promises/promise-1")
    report = c.post("/v1/reports/unfinished/check", json={"items": [{"todo_id": "todo-1"}, {"todo_id": "missing"}]})
    digest = c.get("/v1/digests/2026-11-06")
    assert listed.status_code == item.status_code == 200
    assert listed.json()["data"]["promises"][0]["due_at"] == "2026-11-08"
    assert item.json()["data"]["history"] == [{"version": 1}]
    assert report.json()["ok"] is False and report.json()["items"][1]["reason"] == "todo_not_found"
    assert digest.status_code == 503 and digest.json()["error"]["code"] == "digest_unavailable"


def test_t57_unresolved_operations_are_fail_closed_without_mutating():
    intake = FakeIntake()
    c, _, _ = client(intake=intake)
    response = c.post("/v1/operations/work.cancel", json={"op_id": "cancel-invalid", "payload": {"target": "job-1"}})
    assert response.status_code == 501
    assert response.json()["confirmed"] is False
    assert intake.calls == []


def test_t57_unresolved_operations_reject_valid_payload_without_mutating():
    intake = FakeIntake()
    c, _, _ = client(intake=intake)
    response = c.post("/v1/operations/work.cancel", json={"op_id": "cancel-valid", "payload": {"todo_id": "todo-1", "reason": "cancel"}})
    assert response.status_code == 501
    assert response.json()["confirmed"] is False
    assert intake.calls == []


def test_t57_implemented_operations_still_validate_payloads_before_submitting():
    intake = FakeIntake()
    c, _, _ = client(intake=intake)
    response = c.post("/v1/operations/work.request", json={"op_id": "work-invalid", "payload": {"project": "ax"}})
    assert response.status_code == 422
    assert intake.calls == []


def test_t59_hibi_allowlist_and_method_contract():
    ledger = FakeLedger({
        "/v1/focus-state": {"state": "focus", "since": fresh(20), "as_of": fresh(10)},
        "/v1/daily-state/2026-11-07": {"date": "2026-11-07", "tz": "Asia/Tokyo", "as_of": fresh(10), "segments": [
            {"start": fresh(50), "end": fresh(-50), "state": "focus", "category": "work", "session_title": "implementation"}]},
    })
    c, _, _ = client(ledger)
    now = c.get("/v1/hibi/now")
    today = c.get("/v1/hibi/day", params={"date": "2026-11-07"})
    post = c.post("/v1/hibi/now")
    assert now.status_code == today.status_code == 200
    assert now.json()["state"] == "focus" and now.json()["category"] == "work"
    assert set(today.json()) == {"date", "tz", "as_of", "segments"}
    assert post.status_code == 405 and post.headers["allow"] == "GET"
    public, _, _ = client(ledger, direct=False)
    assert public.get("/v1/hibi/now").status_code == 403


def test_t59_old_or_unavailable_hibi_is_unknown_not_last_value():
    ledger = FakeLedger({
        "/v1/focus-state": {"state": "focus", "since": fresh(300), "as_of": fresh(181)},
        "/v1/daily-state/2026-11-07": {"date": "2026-11-07", "tz": "Asia/Tokyo", "as_of": fresh(181), "segments": []},
    })
    c, _, _ = client(ledger)
    assert c.get("/v1/hibi/now").json()["state"] == "unknown"
    day = c.get("/v1/hibi/day", params={"date": "2026-11-07"}).json()
    assert day["state"] == "unknown" and day["segments"] == []
