"""T-3: 記憶コネクタ。必須項目のない記録を拒否する。秘密を含む記録を拒否する。MCP の保存先は偽（tests/fakes/b1/fake_mcp.py）。"""
import hashlib

import pytest

from axcommon import creds, ids
from axcommon.events import Outbox
from axcommon.redact import Redactor
from tests.fakes.b1.core_memconn_imports import McpClient, checks, service
from tests.fakes.b1.fake_mcp import FakeMcp

SRC = ids.new_ulid(now_ms=1_700_000_000_123)


class Emit:
    def __init__(self):
        self.events = []

    def emit(self, kind, subject, data=None, **kw):
        self.events.append({"kind": kind, "subject": subject, "data": data or {}, **kw})

    def flush(self):
        return 0


def rec(**over):
    r = {"project": "dreamforge", "subloop": "issue-automation", "host": "hub-host", "source": SRC, "kind": "decision",
         "content": "ボス報酬の抽選は、重みつきの表を使う。理由: 偏りを後から調整しやすい。"}
    r.update(over)
    return {k: v for k, v in r.items() if v is not None}


@pytest.fixture
def parts(tmp_path, monkeypatch):
    monkeypatch.setitem(service.SCOPE, "user", "operator")
    mcp = FakeMcp()
    emit = Emit()
    svc = service.MemoryService(McpClient("http://memory.invalid:8377/mcp/", transport=mcp), Redactor(), emit, Outbox(tmp_path / "outbox"))
    return svc, mcp, emit


# --- 必須項目 ---------------------------------------------------------------------------------
@pytest.mark.parametrize("missing", ["project", "host", "source", "kind", "content"])
def test_record_without_a_required_field_is_rejected(parts, missing):
    svc, mcp, emit = parts
    r = svc.remember(rec(**{missing: None}))
    assert r.http == 422 and r.body["error"]["code"] == "missing_fields" and missing in r.body["error"]["fields"]
    assert mcp.calls == [] and mcp.memories == {}                            # 検索も保存もしない


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_required_field_counts_as_missing(parts, blank):
    svc, mcp, _ = parts
    assert svc.remember(rec(project=blank)).body["error"]["code"] == "missing_fields"
    assert mcp.calls == []


def test_subloop_defaults_to_core_and_is_tagged(parts):
    svc, mcp, _ = parts
    r = svc.remember(rec(subloop=None))
    assert r.http == 200 and r.body["status"] == "saved"
    saved = next(iter(mcp.memories.values()))
    assert "subloop:core" in saved["tags"]


@pytest.mark.parametrize("bad,field", [({"kind": "weird"}, "kind"), ({"source": "not-a-ulid"}, "source"), ({"refs": ["bogus"]}, "refs"),
                                       ({"content": "x" * 20001}, "content"), ({"unknown": 1}, "unknown"), ({"project": 5}, "project")])
def test_bad_shapes_are_rejected(parts, bad, field):
    svc, mcp, _ = parts
    r = svc.remember(rec(**bad))
    assert r.http == 422 and r.body["error"]["code"] == "bad_request" and field in r.body["error"]["fields"]
    assert mcp.calls == []


def test_non_object_body_is_rejected(parts):
    svc, mcp, _ = parts
    for body in (None, [], "x", 3):
        assert svc.remember(body).http == 422
    assert mcp.calls == []


# --- 秘密 -------------------------------------------------------------------------------------
def test_secrets_are_rejected_not_redacted_and_never_reach_the_store(parts):
    svc, mcp, emit = parts
    svc.redactor.allow_email_domains = frozenset()
    cases = {
        "token": "メモ: key=" + "s" + "k-" + "A1b2C3d4" * 6,
        "pem": "-----BEGIN " + "PRIVATE KEY-----\nMIIBVgIBADANBgkqhkiG9w0B\n-----END " + "PRIVATE KEY-----",
        "assign": "pass" + "word" + "=" + "hunter2hunter",
        "email": "連絡先 test-user-2@example.com",
        "phone": "電話 090-1234-5678",
        "url": "https://user:" + "s3cretPw99" + "@host.invalid/",
    }
    for name, content in cases.items():
        r = svc.remember(rec(content=content))
        assert r.http == 422 and r.body["error"]["code"] == "secret_detected", name
        assert r.body["error"]["categories"] and "s3cretPw99" not in str(r.body) and "hunter2" not in str(r.body), name
    assert mcp.calls == [] and mcp.memories == {}
    rej = [e for e in emit.events if e["kind"] == "safety.rejected"]
    assert len(rej) == len(cases) and all(e["data"]["reason"] == "secret_detected" for e in rej)
    assert "hunter2hunter" not in str(emit.events)                             # 事象にも値は入れない


def test_secret_in_any_field_is_rejected(parts):
    svc, mcp, _ = parts
    r = svc.remember(rec(host="090-1234-5678"))
    assert r.http == 422 and r.body["error"]["code"] == "secret_detected" and mcp.calls == []


def test_value_from_the_secrets_index_is_rejected(tmp_path):
    from axcommon import redact_index
    sd = tmp_path / "s"
    sd.mkdir()
    (sd / "k").write_text("plain-value-11aa22bb\n", encoding="utf-8")
    idx = tmp_path / "index"
    redact_index.write_index(sd, idx)
    mcp = FakeMcp()
    svc = service.MemoryService(McpClient("http://m.invalid/mcp/", transport=mcp), Redactor(index_path=idx), Emit(), None)
    r = svc.remember(rec(content="これは plain-value-11aa22bb を含む"))
    assert r.http == 422 and r.body["error"]["categories"].get("index:secrets_dir") == 1 and mcp.calls == []


def test_allowed_look_alikes_are_saved(parts):
    svc, mcp, _ = parts
    r = svc.remember(rec(content="コミット " + "a1b2c3d4e5" * 4 + " の変更。連絡は x@example.net へ"))
    assert r.http == 200 and r.body["status"] == "saved"


# --- 保存の形（F-8）---------------------------------------------------------------------------------
def test_saved_shape_follows_f8(parts):
    svc, mcp, emit = parts
    r = svc.remember(rec(refs=["todo:T123"], valid_from="2026-11-05T00:00:00.000Z"))
    assert r.http == 200 and r.body == {"status": "saved", "memory_id": "mem-0001", "enrichment_status": "pending"}
    names = [c[0] for c in mcp.calls]
    assert names == ["memory_search", "memory_remember"]
    search = mcp.calls[0][1]
    assert search["user"] == "operator" and search["project"] == "all" and search["limit"] == 10
    mem = mcp.calls[1][1]["memory"]
    assert mem["type"] == "decision" and mem["visibility"] == "project" and mem["scope"] == {"user": "operator", "project": "all", "agent": None}
    assert mem["source"] == {"agent": "ax", "session": SRC, "uri": f"event:{SRC}"}
    assert mem["valid_from"] == "2026-11-05T00:00:00.000Z"
    assert mem["tags"] == ["project:dreamforge", "subloop:issue-automation", "host:hub-host", "kind:decision", "ax"]
    assert mem["refs"] == [f"event:{SRC}", "todo:T123"]
    d = [e for e in emit.events if e["kind"] == "memory.distilled"]
    assert len(d) == 1 and d[0]["data"] == {"memory_id": "mem-0001", "topic": "dreamforge/issue-automation:decision"} and d[0]["subject"] == "mem-0001"
    assert d[0]["cause"] == SRC and d[0]["project"] == "dreamforge" and d[0]["subloop"] == "issue-automation"


def test_kind_maps_to_memory_type(parts):
    svc, mcp, _ = parts
    for i, k in enumerate(["decision", "observation", "issue", "result", "important_artifact"]):
        svc.remember(rec(kind=k, content=f"種類 {k} の別の内容 {i}-{'x' * i}", source=ids.new_ulid(now_ms=1_700_000_001_000 + i)))
    types = [m["type"] for m in mcp.memories.values()]
    assert types == ["decision", "observation", "issue", "result", "important_artifact"]


# --- 重複と置き換え -------------------------------------------------------------------------------
def test_same_content_is_not_saved_again_and_the_found_id_is_recorded(parts):
    svc, mcp, emit = parts
    first = svc.remember(rec())
    again = svc.remember(rec(source=ids.new_ulid(now_ms=1_700_000_009_000), content="  ボス報酬の抽選は、重みつきの表を使う。理由: 偏りを後から調整しやすい。 "))
    assert again.http == 200 and again.body == {"status": "duplicate", "memory_id": first.body["memory_id"]}
    assert len(mcp.memories) == 1
    d = [e for e in emit.events if e["kind"] == "memory.distilled"]
    assert d[-1]["data"]["memory_id"] == first.body["memory_id"] and d[-1]["data"]["duplicate"] is True


def test_same_refs_count_as_the_same_knowledge(parts):
    svc, mcp, _ = parts
    first = svc.remember(rec(content="原因は X。対処は Y を入れること。"))
    mcp.memories[first.body["memory_id"]]["content"] = "本文は書き換えられている（同じ参照）"
    # 検索が返す記録の refs が同じなら重複（内容が違っても）。検索の対象に入るよう、検索語に当たる内容にしておく
    r = service.find_duplicate([{"id": "m9", "content": "別の文", "refs": [f"event:{SRC}"]}], {"source": SRC, "content": "全く違う内容です", "refs": []})
    assert r == "m9"


def test_supersede_uses_the_old_id_and_is_not_a_duplicate_of_it(parts):
    svc, mcp, emit = parts
    first = svc.remember(rec())
    old_id = first.body["memory_id"]
    new = svc.remember(rec(source=ids.new_ulid(now_ms=1_700_000_010_000), supersedes=old_id,
                           content="ボス報酬の抽選は、重みつきの表を使う。理由: 偏りを後から調整しやすい。更新: 重みは設定で変える。"))
    assert new.http == 200 and new.body["status"] == "superseded" and new.body["memory_id"] != old_id
    name, args = mcp.calls[-1]
    assert name == "memory_supersede" and args["memory_id"] == old_id and args["replacement"]["type"] == "decision"
    assert mcp.memories[old_id]["superseded_by"] == new.body["memory_id"]
    assert "memory_forget" not in [c[0] for c in mcp.calls]                      # 削除はしない


def test_sse_responses_are_read(parts):
    svc, mcp, _ = parts
    mcp.sse = True
    r = svc.remember(rec())
    assert r.http == 200 and r.body["memory_id"] == "mem-0001"


def test_session_is_opened_once_and_reopened_after_expiry(parts):
    svc, mcp, _ = parts
    svc.remember(rec())
    svc.remember(rec(source=ids.new_ulid(now_ms=1_700_000_020_000), content="別の知識 A"))
    assert mcp.initialized == 1
    mcp.expire_session_once()                                                       # セッション切れ（404）→ 開き直して 1 回だけ送り直す
    r = svc.remember(rec(source=ids.new_ulid(now_ms=1_700_000_030_000), content="別の知識 B"))
    assert r.http == 200 and mcp.initialized == 2


# --- 失敗の扱い（F-8、S-14.2）------------------------------------------------------------------------
def test_unreachable_endpoint_queues_to_outbox_and_drains_later(parts, tmp_path):
    svc, mcp, emit = parts
    mcp.refuse = True
    r = svc.remember(rec())
    assert r.http == 202 and r.body["status"] == "queued" and len(svc.outbox) == 1 and mcp.memories == {}
    assert svc.drain_outbox() == 0 and len(svc.outbox) == 1                           # まだ届かない: 残す
    mcp.refuse = False
    assert svc.drain_outbox() == 1 and len(svc.outbox) == 0
    assert list(mcp.memories) == ["mem-0001"]
    assert [e["data"]["memory_id"] for e in emit.events if e["kind"] == "memory.distilled"] == ["mem-0001"]


def test_timeout_after_save_is_unknown_and_is_never_resent(parts):
    svc, mcp, emit = parts
    mcp.drop_after_save = True
    r = svc.remember(rec())
    assert r.http == 202 and r.body["status"] == "unknown" and r.body["memory_id"] is None
    assert len(mcp.memories) == 1                                                       # 相手は保存していた
    assert len(svc.outbox) == 0 and svc.drain_outbox() == 0                              # 再送しない
    mcp.drop_after_save = False
    assert len([c for c in mcp.calls if c[0] == "memory_remember"]) == 1
    d = [e for e in emit.events if e["kind"] == "memory.distilled"]
    assert d[-1]["data"]["state"] == "unknown" and "memory_id" not in d[-1]["data"]


def test_search_timeout_before_saving_does_not_save_blindly(parts):
    svc, mcp, _ = parts
    mcp.timeout_on_search = True
    r = svc.remember(rec())
    assert r.http == 202 and r.body["status"] == "queued" and mcp.memories == {}          # 読み取りの切れは、保存していないので溜めてよい


def test_tool_error_is_reported_and_not_queued(parts):
    svc, mcp, _ = parts
    mcp.error_on_save = True
    r = svc.remember(rec())
    assert r.http == 502 and r.body["error"]["code"] == "memory_error" and len(svc.outbox) == 0


def test_no_endpoint_configured_is_503(tmp_path):
    svc = service.MemoryService(None, Redactor(), Emit(), Outbox(tmp_path / "o"))
    assert svc.remember(rec()).http == 503
    assert svc.remember({}).http == 422                                                  # 検査は接続の前に行う


# --- 検索・読み取り -------------------------------------------------------------------------------
def test_search_get_and_list(parts):
    svc, mcp, _ = parts
    svc.remember(rec())
    r = svc.search({"query": "ボス報酬", "project": "dreamforge", "kind": "decision", "limit": 5})
    assert r.http == 200
    args = mcp.calls[-1][1]
    assert "project:dreamforge" in args["query"] and "kind:decision" in args["query"] and args["limit"] == 5 and args["project"] == "all"
    assert svc.get("mem-0001").body["memory"]["memory"]["type"] == "decision"
    assert svc.get("nope").http == 502
    assert svc.list({"limit": "5"}).http == 200 and mcp.calls[-1][0] == "memory_list"
    assert svc.search({"query": ""}).http == 422 and svc.search({"query": "x", "limit": 0}).http == 422
    assert svc.list({"limit": "x"}).http == 422


def test_connector_never_calls_forget_or_session_tools(parts):
    svc, mcp, _ = parts
    svc.remember(rec())
    svc.search({"query": "x"})
    used = {c[0] for c in mcp.calls}
    assert used <= {"memory_search", "memory_remember", "memory_supersede", "memory_get", "memory_list"}


def test_remember_schemas_match_the_service(parts):
    from tests.b1.helpers import ext_errors
    svc, _m, _e = parts
    assert ext_errors("remember-request", rec()) == []
    assert ext_errors("remember-request", rec(project=None))
    assert ext_errors("remember-request", rec(kind="weird"))
    for body in (svc.remember(rec()).body, svc.remember(rec(project=None)).body):
        if "status" in body:
            assert ext_errors("remember-response", body) == []
