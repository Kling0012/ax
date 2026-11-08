from __future__ import annotations

import hashlib
import json

import pytest

from core.intake.approval import ApprovalError
from core.intake.approval.go import parse_go_md
from tests.fakes.b12b2.intake.parts import destination, go_text, instruction, ratify, removal, seat
from .test_approval import grant, events


def go_request(world, payload, key="go-request"):
    return world["service"].submit(instruction("go.confirm", payload, key=key), destination("go.confirm"), seat())["approval_id"]


def test_T194_ratify_derived_bytes_execution_and_removal_export(world):
    text = go_text(quote="</pre><script>架空の入力</script>")
    aid = go_request(world, ratify(text))
    row = world["store"].get(aid)
    assert json.loads(row["payload_json"])["go_md_text"].encode() == text.encode()
    assert json.loads(row["result_json"])["derived"] == {"scope": "build", "excluded": [], "changes": "all"}
    assert world["service"].view(aid)["data"]["payload"]["go_md_text"] == text
    grant(world, aid)
    ev = events(world, "approval.executed")[-1]
    assert ev["data"]["go"] == {"kind": "ratify", "scope": "build", "changes": "all",
                                  "go_md_sha256": hashlib.sha256(text.encode()).hexdigest()}
    assert text not in json.dumps(ev, ensure_ascii=False)
    assert not world["executor"].calls
    rid = go_request(world, removal(aid), key="removal")
    grant(world, rid)
    exported = world["service"].go.export_view(rid)
    assert exported.startswith("# GO（承認の記録から書き出した物。手で直さない）".encode())
    view = world["service"].go.verify_view(rid, exported, text.encode())
    assert view["checks"] == {"record": "passed", "go_file": "passed", "build_go": "passed"}
    assert view["ratify"] == {"approval_id": aid, "ok": True}
    assert world["service"].go.verify_view(rid, exported.replace(b"digest:", b"Digest:"))["checks"]["go_file"] == "mismatch"
    assert world["service"].go.verify_view(rid, build_go_bytes=text.encode() + b" ")["checks"]["build_go"] == "failed"
    assert world["service"].go.verify_view(aid)["checks"]["record"] == "failed"
    assert b"granted_by" not in exported and b"login" not in exported
    world["service"].withdraw(rid, seat())
    assert world["service"].go.verify_view(rid)["checks"]["record"] == "failed"
    with pytest.raises(ApprovalError, match="verify_failed"):
        world["service"].go.export_view(rid)


@pytest.mark.parametrize("scope,result", [("構築", "build"), ("撤去", "removal"), ("両方", "both")])
def test_T194_scope_parser(scope, result):
    assert parse_go_md(go_text(scope, "1, 3,14")) == {"scope": result, "excluded": [], "changes": [1, 3, 14]}


@pytest.mark.parametrize("text", [
    go_text().replace("# GO", "GO"), go_text().replace("範囲: 構築\n", ""),
    go_text() + "範囲: 構築\n", go_text("未定"), go_text(changes="15"),
    go_text(changes="1,1"), go_text(changes="0"), go_text().replace("\n", "\r\n"),
])
def test_T194_bad_go_text_creates_no_approval(world, text):
    with pytest.raises(ApprovalError, match="go_md_unparsable|bad_request"):
        go_request(world, ratify(text))
    assert not world["store"].list()


@pytest.mark.parametrize("mutation,code", [
    (lambda p: p.update(go_md_sha256="0" * 64), "go_md_hash_mismatch"),
    (lambda p: p.update(scope="both"), "bad_request"),
    (lambda p: p.update(unknown=True), "bad_request"),
])
def test_T194_bad_payload_creates_no_approval(world, mutation, code):
    p = ratify()
    mutation(p)
    with pytest.raises(ApprovalError, match=code):
        go_request(world, p)
    assert not world["store"].list()


def test_T194_size_secret_and_pii_blocks(world):
    world["service"].go.max_bytes = 20
    with pytest.raises(ApprovalError, match="bad_request"):
        go_request(world, ratify())
    world["service"].go.max_bytes = 8192
    text = go_text(quote="sk-" + "x" * 30)
    with pytest.raises(ApprovalError, match="scan_blocked"):
        go_request(world, ratify(text))
    assert not world["store"].list()


@pytest.mark.parametrize("mode", ["missing", "pending", "denied", "expired", "removal_scope", "wrong_kind"])
def test_T194_ineffective_ratification_cannot_authorize_removal(world, mode):
    if mode == "missing":
        aid = "not-found"
    elif mode == "wrong_kind":
        aid = go_request(world, removal(), "first")
        grant(world, aid)
    else:
        aid = go_request(world, ratify(go_text("撤去" if mode == "removal_scope" else "構築")), "first")
        if mode == "denied":
            from tests.fakes.b12b2.intake.parts import HQ
            from core.intake.approval import VerifiedUser
            world["service"].deny(aid, HQ, VerifiedUser(True, True, True))
        elif mode == "expired":
            world["clock"].advance(3601)
            world["service"].refresh(aid)
        elif mode == "removal_scope":
            grant(world, aid)
    before = len(world["store"].list())
    with pytest.raises(ApprovalError, match="ratify_not_effective"):
        go_request(world, removal(aid), "second")
    assert len(world["store"].list()) == before


def test_T194_null_ratification_and_handwritten_go(world):
    aid = go_request(world, removal())
    grant(world, aid)
    assert world["service"].go.verify_view(aid, go_text().encode())["checks"]["go_file"] == "mismatch"
    assert world["service"].go.verify_view(aid, build_go_bytes=go_text().encode())["checks"]["build_go"] == "failed"
    assert world["service"].go.verify_view("not-found")["checks"]["record"] == "failed"


def test_T194_referenced_ratification_does_not_consume_it_again(world):
    aid = go_request(world, ratify(), "ratify")
    grant(world, aid)
    first = go_request(world, removal(aid), "first")
    second = go_request(world, removal(aid), "second")
    assert first != second and world["store"].get(aid)["state"] == "executed"
