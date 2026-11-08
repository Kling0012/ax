"""T-150 切り替えの記録（S-24.1、F-5.5、D-10.1）: tools/cutover_note.py（偽の ax を PATH の先頭に置く）と、オブザーバーの取り込み。

(g) 変更サブループが cutover-time の記録を待つ処理は別の束（変更受付）の試験。ここでは (h) の「最新の 1 件」を選ぶ関数だけを確かめる。
T-208（本物の ax claim との接続）は b12b3。ここでは偽の ax だけを使う。
"""
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from axcommon import schemas
from axcommon.ids import is_ulid
from core.observer.cutover import KINDS, check_line, latest_cutover_time
from tests.b8.helpers import Rig

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "cutover_note.py"
FAKE_AX = ROOT / "tests" / "fakes" / "b8" / "fake_ax.py"
CAUSE = "01HZX0000000000000000000AB"


@pytest.fixture
def bindir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    (d / "ax").symlink_to(FAKE_AX)
    return d


def run_tool(tmp_path, bindir, *args, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_AX")}
    e["PATH"] = f"{bindir}:{e['PATH']}"
    e["AX_CUTOVER_DIR"] = str(tmp_path / "cutover")
    e.update(env or {})
    return subprocess.run([sys.executable, "-I", str(TOOL), *args], capture_output=True, text=True, env=e)


def lines(tmp_path):
    p = tmp_path / "cutover" / "cutover-state.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_cause_constant_is_a_ulid_shape():
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", CAUSE)


# --- (a)(i) 書いた行 ---
def test_a_line_is_json_with_required_fields_and_modes(tmp_path, bindir):
    r = run_tool(tmp_path, bindir, "cutover.state", "issue-automation-old/G3", "G3/compute-host", "ok", "hand-check")
    assert r.returncode == 0, r.stderr
    ev = lines(tmp_path)[0]
    assert is_ulid(ev["id"]) and r.stdout.strip() == ev["id"]
    assert len(ev["id"]) == 26
    assert schemas.errors("event", ev) == []
    assert ev["kind"] == "cutover.state" and ev["subject"] == "issue-automation-old/G3"
    assert ev["project"] is None and ev["subloop"] is None
    assert ev["data"] == {"target": "G3/compute-host", "external_item_id": None, "destination": None, "delivery_state": None,
                          "active_sender": None, "result": "ok", "reason": "hand-check"}
    assert stat.S_IMODE(os.stat(tmp_path / "cutover" / "cutover-state.jsonl").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "cutover").st_mode) == 0o700


def test_i_source_and_cause_come_from_the_ax_response_only(tmp_path, bindir):
    owner = {"principal": "seat-hub1", "session": "cutover-20261107-xyz", "via": "tailnet"}
    r = run_tool(tmp_path, bindir, "cutover.state", "entry-placed", "x", "ok",
                 env={"FAKE_AX_OWNER": json.dumps(owner), "FAKE_AX_CAUSE": CAUSE,
                      "AX_PRINCIPAL": "evil", "AX_SESSION": "evil", "PRINCIPAL": "evil"})
    assert r.returncode == 0, r.stderr
    ev = lines(tmp_path)[0]
    assert ev["source"]["component"] == "cutover-operator"
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{2,31}", ev["source"]["principal"])
    assert ev["source"]["principal"] == "seat-hub1" and ev["source"]["session"] == "cutover-20261107-xyz"
    assert ev["source"]["via"] == "tailnet" and ev["cause"] == CAUSE
    assert "principal" not in ev["data"]


def test_removal_kinds_use_component_id_and_cutover_time_gets_at(tmp_path, bindir):
    assert run_tool(tmp_path, bindir, "removal.state", "stop-hub", "unit-set-a", "ok").returncode == 0
    assert run_tool(tmp_path, bindir, "removal.check", "G11-1", "item-1", "failed", "r").returncode == 0
    assert run_tool(tmp_path, bindir, "cutover.state", "cutover-time", "t", "ok", env={"CUTOVER_AT": "2026-11-08T01:02:03.456Z"}).returncode == 0
    a, b, c = lines(tmp_path)
    assert a["data"] == {"component_id": "unit-set-a", "result": "ok", "reason": ""}
    assert b["kind"] == "removal.check" and b["data"]["result"] == "failed"
    assert c["data"]["at"] == "2026-11-08T01:02:03.456Z"
    assert "at" not in a["data"]


def test_ax_renew_is_called_with_the_documented_arguments(tmp_path, bindir):
    log = tmp_path / "ax.log"
    run_tool(tmp_path, bindir, "cutover.state", "orig-copy", "x", "ok", env={"FAKE_AX_LOG": str(log)})
    argv = json.loads(log.read_text().splitlines()[0])
    assert argv[:4] == ["claim", "renew", "cutover", "--json"] and argv[4] == "--request-id" and is_ulid(argv[5])


# --- 終了コード ---
@pytest.mark.parametrize("args", [(), ("cutover.state", "a", "b"), ("bogus.kind", "a", "b", "ok"),
                                  ("cutover.state", "a", "b", "ok", "r", "extra")])
def test_bad_arguments_exit_2_and_write_nothing(tmp_path, bindir, args):
    r = run_tool(tmp_path, bindir, *args)
    assert r.returncode == 2
    assert not (tmp_path / "cutover" / "cutover-state.jsonl").exists()


def test_line_over_4000_bytes_exits_2(tmp_path, bindir):
    r = run_tool(tmp_path, bindir, "cutover.state", "orig-copy", "x", "ok", "あ" * 1400)
    assert r.returncode == 2
    assert not (tmp_path / "cutover" / "cutover-state.jsonl").exists()


@pytest.mark.parametrize("mode,code", [("conflict", 8), ("conflict_empty", 8), ("missing", 3), ("conflict_json", 8), ("garbage", 3), ("not_holder", 3), ("bad_owner", 3)])
def test_claim_failures_exit_and_write_nothing(tmp_path, bindir, mode, code):
    r = run_tool(tmp_path, bindir, "cutover.state", "orig-copy", "x", "ok", env={"FAKE_AX_MODE": mode})
    assert r.returncode == code, r.stderr
    assert lines(tmp_path) == []


def test_missing_ax_exits_3_and_writes_nothing(tmp_path):
    e = {k: v for k, v in os.environ.items()}
    e["PATH"] = str(tmp_path / "empty")
    e["AX_CUTOVER_DIR"] = str(tmp_path / "cutover")
    r = subprocess.run([sys.executable, "-I", str(TOOL), "cutover.state", "a", "b", "ok"], capture_output=True, text=True, env=e)
    assert r.returncode == 3
    assert lines(tmp_path) == []


# --- (b) 同時の追記 ---
def test_b_concurrent_appends_do_not_mix_lines(tmp_path, bindir):
    e = {k: v for k, v in os.environ.items()}
    e["PATH"] = f"{bindir}:{e['PATH']}"
    e["AX_CUTOVER_DIR"] = str(tmp_path / "cutover")
    ps = [subprocess.Popen([sys.executable, "-I", str(TOOL), "cutover.state", f"s{i}", "t", "ok", "x" * 300],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e) for i in range(10)]
    outs = [p.communicate() for p in ps]
    assert all(p.returncode == 0 for p in ps), outs
    evs = lines(tmp_path)
    assert len(evs) == 10 and len({x["id"] for x in evs}) == 10
    assert sorted(x["subject"] for x in evs) == sorted(f"s{i}" for i in range(10))


# --- 取り込み ---
@pytest.fixture
def rig(tmp_path, fake_clock, fake_transport):
    return Rig(tmp_path, fake_clock, fake_transport)


def good_line(**kw) -> dict:
    ev = {"id": "01HZY0000000000000000000A1", "ts": "2026-11-07T10:00:00.000Z",
          "source": {"component": "cutover-operator", "host": "hub-host", "principal": "hub-main", "session": "cutover-1", "via": "ssh"},
          "kind": "cutover.state", "subject": "orig-copy", "project": None, "subloop": None,
          "data": {"target": "orig-copy/hub-host", "external_item_id": None, "destination": None, "delivery_state": None,
                   "active_sender": None, "result": "ok", "reason": ""}, "cause": CAUSE}
    ev.update(kw)
    return ev


def ulid_n(n: int) -> str:
    return "01HZY" + "0" * 19 + f"{n:02d}"


def append(rig, *objs, raw: list[bytes] | None = None):
    p = rig.state / "cutover" / "cutover-state.jsonl"
    with open(p, "ab") as f:
        for o in objs:
            f.write(json.dumps(o, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
        for r in raw or []:
            f.write(r + b"\n")


def test_c_observer_ingests_lines_and_does_not_duplicate(rig):
    append(rig, good_line(id=ulid_n(1)), good_line(id=ulid_n(2), subject="entry-placed"))
    rig.obs.run_once()
    got = rig.tl.of_kind("cutover.state")
    assert [e["id"] for e in got] == [ulid_n(1), ulid_n(2)]
    rig.obs.run_once()
    assert rig.tl.posts == 2  # 取り込み済みの行数で飛ばす
    (rig.data / "cutover.cursor").rename(rig.data / "cutover.cursor.saved")  # 取り込み済みの数を消しても同じ ID は 1 件
    rig.obs.run_once()
    assert len(rig.tl.of_kind("cutover.state")) == 2
    assert rig.tl.posts == 4


def test_c_tool_output_is_ingested_end_to_end(rig, tmp_path, bindir):
    r = run_tool(tmp_path, bindir, "cutover.state", "orig-copy", "orig-copy/hub-host", "ok", env={"AX_CUTOVER_DIR": str(rig.state / "cutover")})
    assert r.returncode == 0, r.stderr
    rig.obs.run_once()
    assert [e["id"] for e in rig.tl.of_kind("cutover.state")] == [r.stdout.strip()]


def test_d_broken_and_disallowed_lines_are_not_ingested_and_others_continue(rig):
    bad_kind = good_line(id=ulid_n(3), kind="run.state")
    no_session = good_line(id=ulid_n(4)); del no_session["source"]["session"]
    bad_principal = good_line(id=ulid_n(5)); bad_principal["source"]["principal"] = "X"
    bad_cause = good_line(id=ulid_n(6), cause="nope")
    no_cause = good_line(id=ulid_n(7)); del no_cause["cause"]
    with_principal = good_line(id=ulid_n(8)); with_principal["data"]["principal"] = "hub-main"
    bad_via = good_line(id=ulid_n(9)); bad_via["source"]["via"] = "carrier-pigeon"
    append(rig, good_line(id=ulid_n(1)), bad_kind, no_session, bad_principal, bad_cause, no_cause, with_principal, bad_via,
           raw=[b"{not json", b"[1,2]"])
    append(rig, good_line(id=ulid_n(2)))
    rig.obs.run_once()
    assert [e["id"] for e in rig.tl.of_kind("cutover.state")] == [ulid_n(1), ulid_n(2)]
    errs = rig.tl.of_kind("error.classified")
    assert len(errs) == 9 and all(e["data"]["code"] == "cutover_record_invalid" for e in errs)
    assert all(e["data"]["class"] in ("artifact.bug", "system.expected", "unknown") for e in errs)
    rig.obs.run_once()
    assert len(rig.tl.of_kind("error.classified")) == 9  # 同じ壊れた行を何度も報告しない


def test_d_line_of_4000_bytes_or_more_is_rejected():
    ev = good_line()
    ev["data"]["reason"] = "x" * 4000
    assert check_line(json.dumps(ev).encode())[1] == "line_too_long"
    assert set(KINDS) == {"cutover.state", "removal.state", "removal.check"}


def test_e_append_works_while_timeline_is_down_and_is_ingested_in_order_after(rig):
    rig.tl.stop()
    append(rig, good_line(id=ulid_n(1)))
    rig.obs.run_once()
    append(rig, good_line(id=ulid_n(2)))  # 止まっている間も追記は成功する
    rig.obs.run_once()
    assert rig.tl.events == []
    rig.tl.start()
    rig.obs.run_once()
    assert [e["id"] for e in rig.tl.of_kind("cutover.state")] == [ulid_n(1), ulid_n(2)]


def test_e_unfinished_last_line_waits_for_the_next_cycle(rig):
    append(rig, good_line(id=ulid_n(1)))
    with open(rig.state / "cutover" / "cutover-state.jsonl", "ab") as f:
        f.write(json.dumps(good_line(id=ulid_n(2))).encode()[:40])  # 書きかけ（改行なし）
    rig.obs.run_once()
    assert [e["id"] for e in rig.tl.of_kind("cutover.state")] == [ulid_n(1)]
    assert rig.tl.of_kind("error.classified") == []


def test_f_secret_like_reason_is_redacted_on_ingest(tmp_path, fake_clock, fake_transport):
    from axcommon.redact import Redactor

    rig = Rig(tmp_path, fake_clock, fake_transport, redact=Redactor().as_hook())
    secret = "sk-" + "a1B2c3D4e5" * 4  # 実行時に連結して作る（試験のソースに秘密の形の値を置かない）
    ev = good_line(id=ulid_n(1))
    ev["data"]["reason"] = f"token {secret} was pasted"
    append(rig, ev)
    rig.obs.run_once()
    got = rig.tl.of_kind("cutover.state")[0]
    assert secret not in json.dumps(got) and "[REDACTED]" in got["data"]["reason"]


def test_h_latest_cutover_time_is_the_newest_by_ts():
    def e(ts, at, subject="cutover-time"):
        return {"kind": "cutover.state", "subject": subject, "ts": ts, "data": {"at": at}}

    evs = [e("2026-11-07T10:00:00.000Z", "A"), e("2026-11-08T10:00:00.000Z", "B"), e("2026-11-07T12:00:00.000Z", "C"),
           e("2026-11-09T10:00:00.000Z", "Z", subject="orig-copy")]
    assert latest_cutover_time(evs) == "B"
    assert latest_cutover_time([]) is None


def test_cutover_dir_is_the_only_input_and_missing_file_is_fine(rig):
    (rig.state / "cutover").rename(rig.state / "cutover.saved")
    rig.obs.run_once()
    assert rig.tl.events == [] or all(e["kind"] != "cutover.state" for e in rig.tl.events)


def test_cutover_at_is_not_written_to_other_subjects_or_removal(tmp_path, bindir):
    for kind, subject in (("cutover.state", "orig-copy"), ("removal.check", "cutover-time")):
        r = run_tool(tmp_path, bindir, kind, subject, "x", "ok", env={"CUTOVER_AT": "2026-11-08T01:02:03.456Z"})
        assert r.returncode == 0, r.stderr
    assert all("at" not in e["data"] for e in lines(tmp_path))


def test_existing_cutover_permissions_are_repaired(tmp_path, bindir):
    directory = tmp_path / "cutover"
    directory.mkdir(mode=0o755)
    p = directory / "cutover-state.jsonl"
    p.touch(mode=0o644)
    r = run_tool(tmp_path, bindir, "cutover.state", "orig-copy", "x", "ok")
    assert r.returncode == 0, r.stderr
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


@pytest.mark.parametrize("owner", [[], "invalid", {"principal": "hub-main", "session": "", "via": "ssh"}])
def test_invalid_claim_owner_exits_3(tmp_path, bindir, owner):
    r = run_tool(tmp_path, bindir, "cutover.state", "orig-copy", "x", "ok", env={"FAKE_AX_OWNER": json.dumps(owner)})
    assert r.returncode == 3, r.stderr
    assert lines(tmp_path) == []


@pytest.mark.parametrize("field,value", [("target", None), ("result", "unrecognized"), ("reason", ["bad"])])
def test_malformed_cutover_data_is_not_ingested(rig, field, value):
    ev = good_line()
    ev["data"][field] = value
    append(rig, ev)
    rig.obs.run_once()
    assert rig.tl.of_kind("cutover.state") == []
    assert rig.tl.of_kind("error.classified")[0]["data"]["code"] == "cutover_record_invalid"


def test_missing_redactor_still_checks_secrets(rig):
    secret = "sk-" + "abcdefgh12345678" * 4
    ev = good_line()
    ev["data"]["reason"] = secret
    append(rig, ev)
    rig.obs.run_once()
    assert rig.tl.of_kind("cutover.state")[0]["data"]["reason"] == "[REDACTED]"


def test_redactor_failure_defers_only_cutover_ingest(rig):
    def broken(value):
        raise ValueError("redactor unavailable")
    rig.obs.cutover._redact = broken
    append(rig, good_line())
    rig.obs.run_once()
    assert rig.tl.of_kind("cutover.state") == []
    assert (rig.state / "observer.beat").exists()
    rig.obs.cutover._redact = lambda value: value
    rig.obs.run_once()
    assert len(rig.tl.of_kind("cutover.state")) == 1


@pytest.mark.parametrize("at", ["", "tomorrow", "2026-02-31T01:02:03.456Z"])
def test_cutover_time_requires_valid_utc_milliseconds(tmp_path, bindir, at):
    r = run_tool(tmp_path, bindir, "cutover.state", "cutover-time", "t", "ok", env={"CUTOVER_AT": at})
    assert r.returncode == 2
    assert lines(tmp_path) == []


def test_cutover_time_without_at_is_invalid_on_ingest():
    ev = good_line(subject="cutover-time")
    assert check_line(json.dumps(ev).encode())[1] == "bad_cutover_time"


def test_overflowing_ulid_in_claim_reply_is_rejected(tmp_path, bindir):
    r = run_tool(tmp_path, bindir, "cutover.state", "orig-copy", "x", "ok", env={"FAKE_AX_CAUSE": "Z" * 26})
    assert r.returncode == 3
    assert lines(tmp_path) == []
