"""T-1: 時系列記録。追記・照会・購読、書き換えと削除の API が無いこと、再起動後のカーソル、同じ id の再送、秘密の置き換え。
starlette が無い環境では HTTP の部分を飛ばす（Store の部分は飛ばさない）。"""
import hashlib
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from axcommon import creds, redact_index
from axcommon.redact import REDACTED, Redactor
from core.timeline import kinds
from core.timeline.store import Store
from tests.b1.helpers import ext_errors
from tests.fakes.b1.events import make_event

CORE_ROOT = Path(__file__).resolve().parents[2] / "core" / "timeline"


# --- Store（HTTP を使わない） -----------------------------------------------------------------
def test_append_assigns_increasing_seq_and_reads_back(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    evs = [make_event("run.state", f"R{i}", {"state": "running"}) for i in range(3)]
    res = s.append(evs)
    assert [r["seq"] for r in res] == [1, 2, 3] and not any(r["duplicate"] for r in res)
    got = s.list_events(after=0)
    assert [e["seq"] for e in got] == [1, 2, 3] and got[0]["id"] == evs[0]["id"]
    assert [e["seq"] for e in s.list_events(after=1)] == [2, 3]          # カーソルの続き
    assert s.get_event(evs[1]["id"])["seq"] == 2 and s.get_event("nope") is None


def test_same_id_is_ignored_and_reported_as_duplicate(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    e = make_event("run.state", "R1", {"state": "running"})
    assert s.append([e])[0]["duplicate"] is False
    again = s.append([e])
    assert again == [{"id": e["id"], "seq": 1, "duplicate": True}]
    assert len(s.list_events()) == 1 and s.max_seq() == 1


def test_cursor_survives_restart(tmp_path):
    p = tmp_path / "t.sqlite"
    s = Store(p)
    s.append([make_event("run.state", "R1", {"state": "running"}), make_event("run.state", "R2", {"state": "running"})])
    s.close()
    s2 = Store(p)                                                          # 再起動
    assert [e["seq"] for e in s2.list_events(after=1)] == [2]
    r = s2.append([make_event("run.state", "R3", {"state": "running"})])
    assert r[0]["seq"] == 3                                                # seq は続きから


def test_filters(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.append([make_event("run.state", "R1", {"state": "running"}, subloop="a", project="p1"),
              make_event("job.state", "pool", {"job_id": "J", "state": "running"}, subloop="b", project="p2"),
              make_event("run.state", "R2", {"state": "failed"}, subloop="a", project="p2")])
    assert len(s.list_events(kinds=["run.state"])) == 2
    assert len(s.list_events(kinds=["run.state", "job.state"])) == 3
    assert [e["subject"] for e in s.list_events(subloop="a")] == ["R1", "R2"]
    assert [e["subject"] for e in s.list_events(project="p2")] == ["pool", "R2"]
    assert [e["subject"] for e in s.list_events(subject="R2")] == ["R2"]
    assert len(s.list_events(limit=2)) == 2


def test_db_refuses_update_and_delete_of_events(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.append([make_event("run.state", "R1", {"state": "running"})])
    con = sqlite3.connect(tmp_path / "t.sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE events SET subject='x'")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("DELETE FROM events")


def test_source_has_no_update_or_delete_sql_for_events():
    """更新・削除の SQL を書く経路をコードに持たない（D-3、T-1）。projections と meta の作り直しは対象外。"""
    bad = re.compile(r"\b(UPDATE|DELETE\s+FROM|INSERT\s+OR\s+REPLACE|REPLACE\s+INTO|DROP\s+TABLE|ALTER\s+TABLE)\s+events\b", re.I)
    for p in CORE_ROOT.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for m in bad.finditer(text):
            # トリガーの定義（拒否する側）だけは許す
            ctx = text[max(0, m.start() - 80): m.end() + 80]
            assert "RAISE(ABORT" in ctx or "CREATE TRIGGER" in ctx, f"{p.name}: {m.group(0)}"


def test_redacts_secret_forms_in_data_on_write(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    token = "s" + "k-" + "A1b2C3d4" * 6
    e = make_event("error.classified", "x", {"message": "key " + token + " here", "n": 3, "nested": {"t": "tel 090-1234-5678"}})
    s.append([e], redactor=Redactor().as_hook())
    got = s.list_events()[0]["data"]
    assert token not in str(got) and got["message"] == "key " + REDACTED + " here" and got["n"] == 3
    assert got["nested"]["t"] == "tel " + REDACTED
    assert "x@example.net" in Redactor().redact_text("x@example.net").text


def test_index_value_is_redacted_anywhere_in_data(tmp_path):
    sd = tmp_path / "secrets"
    sd.mkdir()
    secret = "plain-" + "value-" + "11aa22bb"
    (sd / "k").write_text(secret + "\n", encoding="utf-8")
    idx = tmp_path / "state" / "redact" / "index"
    redact_index.write_index(sd, idx)
    s = Store(tmp_path / "t.sqlite")
    s.append([make_event("error.classified", "x", {"message": "see " + secret + "!"})], redactor=Redactor(index_path=idx).as_hook())
    assert secret not in str(s.list_events()[0]["data"])
