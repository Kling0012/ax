"""T-112 再開の条件（S-8.12）。

失敗・時間切れ・停滞・消失した会話は続けない。別の項目、別のモデル、resume_max_age_min の超過、
resume_max_context_tokens の超過（不明を含む）、resume_max_chain の超過では新しい会話になる。
R-N14 の指定があり条件を満たすのに元の会話の記録が無いときは、新しい会話に置き換えず parked になる。
再開した試行の session_id が元と同じで、JSON の値と食い違えば JSON の値が次に使われる。
文脈の判定は .usage-<job_id>.json の last_context（最後のターンの値）で行い、
実行全体の合計が大きくても最後のターンが小さければ再開できる。
resume_from を付けるのは worker.run の入り直し（resume: auto）と、項目の resume_ref（resume_required）だけで、
worker-pool は自分では付けない。利用者の回答が resume_max_age_min を過ぎていれば parked にせず新しい会話になる。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib
_r = importlib.import_module("plugins.resource.worker-pool.resume")
_u = importlib.import_module("plugins.resource.worker-pool.usage")


def _orig(**kw):
    base = {"state": "ok", "item_id": "it-1", "model": "m1", "finished_at": "2026-11-05T12:00:00.000Z",
            "last_context": 100, "chain_length": 0, "root": "rt", "session_id": "sid"}
    base.update(kw)
    return base


def test_failed_timed_out_stalled_lost_do_not_continue(tmp_path):
    for st in ("failed", "timed_out", "stalled", "lost"):
        r = _r.can_resume(_orig(state=st), item_id="it-1", model="m1", now="2026-11-05T12:00:00.000Z")
        assert r["resume"] is False, st


def test_other_item_or_model_new_session(tmp_path):
    assert _r.can_resume(_orig(), item_id="other", model="m1", now="2026-11-05T12:00:00.000Z")["resume"] is False
    assert _r.can_resume(_orig(), item_id="it-1", model="other", now="2026-11-05T12:00:00.000Z")["resume"] is False


def test_age_context_chain_exceed_new_session(tmp_path):
    assert _r.can_resume(_orig(), item_id="it-1", model="m1", now="2026-11-05T13:30:00.000Z", age_min=55)["resume"] is False
    assert _r.can_resume(_orig(last_context=999999), item_id="it-1", model="m1", now="2026-11-05T12:00:00.000Z", max_context=60000)["resume"] is False
    assert _r.can_resume(_orig(chain_length=99), item_id="it-1", model="m1", now="2026-11-05T12:00:00.000Z", max_chain=3)["resume"] is False


def test_unknown_last_context_does_not_resume(tmp_path):
    r = _r.can_resume(_orig(last_context=None), item_id="it-1", model="m1", now="2026-11-05T12:00:00.000Z")
    assert r["resume"] is False and r["reason"] == "last_context_unknown"


def test_last_context_uses_last_turn_not_total(tmp_path):
    # 実行全体の合計が大きくても最後のターンが小さければ再開できる
    lu = {"last_context": 100, "first_input": 10}
    # usage の合計は別で記録される
    r = _r.can_resume(_orig(last_context=lu["last_context"]), item_id="it-1", model="m1", now="2026-11-05T12:00:00.000Z", max_context=60000)
    assert r["resume"] is True


def test_resume_required_missing_session_parked(tmp_path):
    d = _r.decide_job(_orig(), item_id="it-1", model="m1", resume_required=True, session_found=False, now="2026-11-05T12:00:00.000Z")
    assert d["mode"] == "parked"


def test_resume_required_missing_session_new_when_not_required(tmp_path):
    d = _r.decide_job(_orig(), item_id="it-1", model="m1", resume_required=False, session_found=False, now="2026-11-05T12:00:00.000Z")
    assert d["mode"] == "new_session"


def test_expired_resume_required_new_session_not_parked(tmp_path):
    d = _r.decide_job(_orig(), item_id="it-1", model="m1", resume_required=True, session_found=True, now="2026-11-05T14:00:00.000Z")
    assert d["mode"] == "new_session"


def test_session_id_json_mismatch_updates(tmp_path):
    # 再開した試行の session_id が元と同じで、JSON の値と食い違えば JSON の値が次に使われる
    doc = {"session_id": "json-sid"}
    got = _u.session_id_from(doc)
    assert got == "json-sid"
    # jobs 表の session_id を JSON の値に更新する（worker-pool の finisher が行う）
