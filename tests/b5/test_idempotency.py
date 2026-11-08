"""依頼 ID の照会（S-6.16 の核）と監査ログの試験。"""
from __future__ import annotations

from core.intake.audit import AccessLog
from core.intake.idempotency import DONE, PENDING, IdempotencyStore


def test_begin_finish_and_replay_from_file(tmp_path):
    p = tmp_path / "idem.jsonl"
    s = IdempotencyStore(p, retain_h=24.0)
    s.begin("control.command", "k-1", {"a": 1}, "local-operator")
    rec = s.lookup("control.command", "k-1", {"a": 1})
    assert rec["state"] == PENDING and rec["mismatch"] is False
    s.finish("control.command", "k-1", status=200, result={"done": True})
    rec = s.lookup("control.command", "k-1", {"a": 1})
    assert rec["state"] == DONE and rec["result"] == {"done": True}

    s2 = IdempotencyStore(p, retain_h=24.0)  # 作り直しても覚えている
    assert s2.lookup("control.command", "k-1", {"a": 1})["result"] == {"done": True}
    assert s2.lookup("control.command", "k-1", {"a": 2})["mismatch"] is True
    assert s2.lookup("control.command", "k-2", {"a": 1}) is None  # 組（destination, key）


def test_first_principal_is_kept(tmp_path):
    s = IdempotencyStore(tmp_path / "idem.jsonl", retain_h=24.0)
    s.begin("d", "k", {}, "seat-a")
    s.finish("d", "k", status=200, result={"r": 1})
    again = s.begin("d", "k", {}, "seat-b")  # 再送（別の席から）。既にある組は上書きしない
    assert again["state"] == DONE and again["principal"] == "seat-a" and again["result"] == {"r": 1}


def test_expired_records_are_forgotten(tmp_path, fake_clock):
    p = tmp_path / "idem.jsonl"
    s = IdempotencyStore(p, retain_h=24.0)
    s.begin("d", "old", {}, "x")
    s.finish("d", "old", status=200, result={"old": True})
    fake_clock.advance(25 * 3600)
    s.begin("d", "new", {}, "x")
    assert s.lookup("d", "old", {}) is None  # 保持期間を過ぎた組は忘れる
    assert s.lookup("d", "new", {}) is not None


def test_access_log_appends_json_lines(tmp_path):
    p = tmp_path / "access.log"
    log = AccessLog(p)
    log.write(ts="2026-11-05T12:00:00.000Z", via="local", principal="local-operator",
              method="POST", path="/v1/instructions", status=200)
    log.write(ts="2026-11-05T12:00:01.000Z", via="local", principal=None,
              method="CONNECT", path="-", status=403)
    lines = log.read_all()
    assert [l["method"] for l in lines] == ["POST", "CONNECT"]
    assert lines[0]["principal"] == "local-operator" and lines[1]["status"] == 403
