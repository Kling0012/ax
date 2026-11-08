"""events の組み立てと送信、outbox（S-2.2、S-3.2、D-2.2）と client（B0）。"""
import json

import pytest

from axcommon import events, ids
from axcommon.client import ApiClient, ApiError, ServerError, TimelineClient, Unavailable, service_url
from axcommon.events import EventSink, Outbox, build_event
from axcommon import schemas


def _sink(tmp_path, transport):
    return EventSink(TimelineClient("http://timeline:8081", token="plugin-token-xyz", transport=transport), "issue-automation", "hub-host", tmp_path / "outbox",
                     project="example-project", subloop="issue-automation")


# ---- build_event（S-3.2）----
def test_build_event_has_ulid_utc_ts_and_required_fields(fake_clock):
    ev = build_event(component="issue-automation", host="hub-host", kind="run.state", subject="run1", data={"state": "running"})
    assert ids.is_ulid(ev["id"])
    assert ev["ts"] == "2026-11-05T12:00:00.000Z"
    assert ev["source"] == {"component": "issue-automation", "host": "hub-host"}
    assert ev["project"] is None and ev["subloop"] is None and ev["cause"] is None


def test_build_event_rejects_malformed_event():
    with pytest.raises(schemas.ValidationFailed):
        build_event(component="c", host="h", kind="run.state", subject="s", data={"state": "nonsense"})


def test_build_event_source_extra_carries_principal_session_via():
    ev = build_event(component="intake", host="hub-host", kind="instruction.received", subject="s", data={},
                     source_extra={"principal": "local-operator", "session": "sess1", "via": "local"})
    assert ev["source"]["principal"] == "local-operator" and ev["source"]["via"] == "local"


def test_build_event_rejects_unknown_via():
    with pytest.raises(schemas.ValidationFailed):
        build_event(component="intake", host="h", kind="instruction.received", subject="s", data={}, source_extra={"via": "carrier-pigeon"})


def test_redactor_hook_applied_to_data_before_anything_is_stored(tmp_path, fake_transport):
    fake_transport.down()
    events.set_redactor(lambda d: {k: ("[REDACTED]" if k == "secret" else v) for k, v in d.items()})
    try:
        s = _sink(tmp_path, fake_transport)
        s.emit("error.classified", "x", {"class": "unknown", "code": "unclassified", "secret": "value-should-not-persist"})
    finally:
        events.set_redactor(None)
    stored = json.dumps(s.outbox.read_all())
    assert "value-should-not-persist" not in stored and "[REDACTED]" in stored


# ---- 送信と outbox（S-2.2）----
def test_emit_posts_to_timeline_with_bearer_and_no_outbox(tmp_path, fake_transport):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    s = _sink(tmp_path, fake_transport)
    ev = s.emit("run.state", "run1", {"state": "running"})
    call = fake_transport.calls[0]
    assert call.url == "http://timeline:8081/v1/events"
    assert call.headers["Authorization"] == "Bearer plugin-token-xyz"
    assert call.json["id"] == ev["id"] and call.json["subloop"] == "issue-automation" and call.json["project"] == "example-project"
    assert len(s.outbox) == 0


def test_timeline_down_never_raises_and_events_wait_in_outbox(tmp_path, fake_transport):
    fake_transport.down()
    s = _sink(tmp_path, fake_transport)
    for i in range(3):
        s.emit("run.state", f"run{i}", {"state": "running"})  # 例外が出ない（S-2.2: 届かないことを理由に処理を止めない）
    assert [e["body"]["subject"] for e in s.outbox.read_all()] == ["run0", "run1", "run2"]
    assert s.queued_total == 3


def test_5xx_from_timeline_is_treated_as_unavailable(tmp_path, fake_transport):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    fake_transport.fail_next(1)
    s = _sink(tmp_path, fake_transport)
    s.emit("run.state", "r", {"state": "running"})
    assert len(s.outbox) == 1


def test_recovery_flushes_in_order_and_keeps_sent_copy(tmp_path, fake_transport):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    fake_transport.down()
    s = _sink(tmp_path, fake_transport)
    for i in range(3):
        s.emit("run.state", f"run{i}", {"state": "running"})
    fake_transport.calls.clear()
    fake_transport.up()
    assert s.flush() == 3
    assert [c.json["subject"] for c in fake_transport.calls] == ["run0", "run1", "run2"]
    assert len(s.outbox) == 0
    sent_files = list((tmp_path / "outbox" / "sent").glob("*.jsonl"))
    assert sent_files and len(sent_files[0].read_text().splitlines()) == 3  # 削除せず移す（F-1）


def test_new_event_after_recovery_goes_after_the_backlog(tmp_path, fake_transport):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    fake_transport.down()
    s = _sink(tmp_path, fake_transport)
    s.emit("run.state", "old1", {"state": "running"})
    s.emit("run.state", "old2", {"state": "running"})
    fake_transport.calls.clear()
    fake_transport.up()
    s.emit("run.state", "new", {"state": "completed"})
    assert [c.json["subject"] for c in fake_transport.calls] == ["old1", "old2", "new"]


def test_partial_flush_stops_at_first_failure_and_keeps_the_rest(tmp_path, fake_transport):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    fake_transport.down()
    s = _sink(tmp_path, fake_transport)
    for i in range(3):
        s.emit("run.state", f"r{i}", {"state": "running"})
    fake_transport.up()
    state = {"n": 0}
    orig = fake_transport.__call__

    def flaky(method, url, headers, body, timeout):
        state["n"] += 1
        if state["n"] == 2:
            raise Unavailable("ConnectError")
        return orig(method, url, headers, body, timeout)

    s.client._transport = flaky
    assert s.flush() == 1
    assert [e["body"]["subject"] for e in s.outbox.read_all()] == ["r1", "r2"]


def test_permanently_rejected_entry_moves_to_rejected_and_does_not_block_the_queue(tmp_path, fake_transport):
    s = _sink(tmp_path, fake_transport)
    fake_transport.down()
    s.emit("run.state", "bad", {"state": "running"})
    s.emit("run.state", "good", {"state": "running"})
    fake_transport.up()
    fake_transport.add("POST", "/v1/events", handler=lambda c: (422, {}, b'{"error":{"code":"bad_request","message":"x"}}') if c.json["subject"] == "bad" else (200, {}, b"{}"))
    assert s.flush() == 2
    assert len(s.outbox) == 0
    assert list((tmp_path / "outbox" / "rejected").glob("*.jsonl"))


def test_outbox_survives_restart_and_ignores_torn_lines(tmp_path):
    ob = Outbox(tmp_path / "ob")
    ob.append("timeline", "/v1/events", "POST", {"a": 1})
    with open(ob.pending, "a") as f:
        f.write('{"target": "timeline", "path"')  # 落ちた時の書きかけ
    again = Outbox(tmp_path / "ob")
    assert [e["body"] for e in again.read_all()] == [{"a": 1}]


def test_outbox_entries_match_schema(tmp_path):
    ob = Outbox(tmp_path / "ob")
    e = ob.append("timeline", "/v1/events", "POST", {"x": 1})
    assert schemas.errors("outbox-entry", e) == []


def test_sink_without_client_only_queues(tmp_path):
    s = EventSink(None, "c", "h", tmp_path / "ob")
    s.emit("health.beat", "c", {"uptime_s": 1})
    assert len(s.outbox) == 1 and s.flush() == 0


# ---- client ----
def test_client_maps_status_classes(fake_transport):
    fake_transport.add("GET", "/ok", json={"a": 1})
    fake_transport.add("GET", "/bad", status=403, json={"error": {"code": "plugin_not_allowed", "message": "no"}})
    fake_transport.add("GET", "/boom", status=500)
    c = ApiClient("http://x:8080", transport=fake_transport)
    assert c.get("/ok") == {"a": 1}
    with pytest.raises(ApiError) as ei:
        c.get("/bad")
    assert (ei.value.status, ei.value.code) == (403, "plugin_not_allowed")
    with pytest.raises(ServerError):
        c.get("/boom")
    fake_transport.down()
    with pytest.raises(Unavailable):
        c.get("/ok")


def test_client_never_prints_token_and_validates_base_url():
    c = ApiClient("http://x", token="supersecrettoken")
    assert "supersecrettoken" not in repr(c) and "supersecrettoken" not in str(c.__dict__.get("base_url"))
    with pytest.raises(ValueError):
        ApiClient("ftp://x")


def test_client_params_skip_none_and_encode(fake_transport):
    fake_transport.add("GET", "/v1/events", json=[])
    TimelineClient("http://t:8080", transport=fake_transport).get_events(after=5, kind="run.state", limit=None, wait=30)
    q = fake_transport.calls[0].query
    assert "after=5" in q and "kind=run.state" in q and "wait=30" in q and "limit" not in q


def test_client_without_token_sends_no_authorization(fake_transport):
    fake_transport.add("GET", "/healthz", json={"ok": True})
    ApiClient("http://t:8080", transport=fake_transport).healthz()
    assert "Authorization" not in fake_transport.calls[0].headers


def test_service_url_default_and_env_override():
    assert service_url("timeline", 8081, environ={}) == "http://timeline:8081"
    assert service_url("memory-connector", environ={"AX_MEMORY_CONNECTOR_URL": "http://other:1"}) == "http://other:1"
