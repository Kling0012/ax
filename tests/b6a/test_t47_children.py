"""T-47 子の評価と貸出（S-8.9）。

下位の空き枠を貸し出し、不能でも親は継続する。全終了経路で子の枠を解放する。
曖昧な評価は保留し、会話全文を記録しない。違反通知と抽出監査を記録する。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.b6a.fixtures import default_models
import importlib
_ch = importlib.import_module("plugins.resource.worker-pool.children")


def test_lend_lower_slot_parent_continues_when_cannot(tmp_path):
    models = default_models()
    jobs = []
    r = _ch.lendable_slots(parent_level="L3", candidates=models, jobs=jobs)
    assert r["ok"] is True and r["options"]
    # 空きが無ければ理由を返し、親を止めない
    jobs_full = [{"model": "our-free-model/a-free", "state": "running"}]
    r2 = _ch.lendable_slots(parent_level="L1", candidates=[m for m in models if m["level"] == "L1" and m["id"] == "our-free-model/a-free"], jobs=jobs_full)
    assert r2["ok"] is False and r2["reason"] == "no_lower_slot"
    # 親は継続できる（父のジョブ自体の選べる）


def test_child_evaluation_pass_fail_pending(tmp_path):
    assert _ch.evaluate_child(verify_rc=0)["verdict"] == "pass"
    assert _ch.evaluate_child(verify_rc=1)["verdict"] == "fail"
    assert _ch.evaluate_child(verify_rc=None)["verdict"] == "pending"
    # 利用制限は能力上の失敗に数えない
    assert _ch.evaluate_child(verify_rc=1, rate_limited=True)["verdict"] == "pending"
    # 曖昧なら pending
    assert _ch.evaluate_child(verify_rc=0, ambiguous=True)["verdict"] == "pending"


def test_child_evaluation_does_not_copy_full_transcript(tmp_path):
    doc = _ch.child_event_data(parent_job_id="p1", child_id="c1", model="m1", verdict={"verdict": "pass", "reason": "verify_ok"})
    assert "transcript" not in doc and "conversation" not in doc
    assert doc["verdict"] == "pass"


def test_audit_sample_ten_percent_ceil(tmp_path):
    assert _ch.audit_sample([]) == {"sampled": 0, "total": 0}
    assert _ch.audit_sample([{"id": str(i)} for i in range(5)])["sampled"] == 1  # ceil adjustment
    assert _ch.audit_sample([{"id": str(i)} for i in range(21)])["sampled"] == 3
