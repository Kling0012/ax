"""T-45 能力評価（S-8.5）。

利用制限・接続障害は不合格に数えず一時除外する。無料モデルの直近失敗と成功率を判定する。
GLM・GPT も実績に従い降格し、役割の最低段階を守る。未昇格無料モデルに昇格試行で実装を割り当てない。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.resources import promotion
from tests.b6a.fixtures import resources_store, seed_models


def _record(store, rid, outcomes):
    for o, caf in outcomes:
        store.record_outcome(rid, outcome=o, counts_as_failure=caf, verify_rc=0 if o == "pass" else 1, error_class="ok" if caf in ("yes", "no") else "rate_limit", now="2026-11-05T12:00:00.000Z")


def test_new_free_model_starts_l1(tmp_path):
    store = resources_store(tmp_path)
    seed_models(store)
    m = store.get("our-free-model/a-free")
    assert m["level"] == "L1"


def test_promote_after_streak(tmp_path):
    store = resources_store(tmp_path)
    seed_models(store)
    _record(store, "our-free-model/a-free", [("pass", "no"), ("pass", "no"), ("pass", "no")])
    r = promotion.evaluate(store, "our-free-model/a-free", promote_after=3, demote_after=2, now="2026-11-05T12:00:00.000Z")
    assert r["new_level"] == "L2"


def test_demote_after_streak_glm_gpt(tmp_path):
    store = resources_store(tmp_path)
    seed_models(store)
    # GLM も降格する（S-8.5）
    _record(store, "zcode/GLM-5.3", [("fail", "yes"), ("fail", "yes")])
    r = promotion.evaluate(store, "zcode/GLM-5.3", promote_after=3, demote_after=2, now="2026-11-05T12:00:00.000Z")
    assert r["new_level"] == "L2"


def test_rate_limit_is_not_a_failure_and_temporary_exclude(tmp_path):
    store = resources_store(tmp_path)
    seed_models(store)
    store.record_outcome("our-free-model/b-free", outcome="fail", counts_as_failure="hold", error_class="rate_limit",
                         job_id="job-1", now="2026-11-05T12:00:00.000Z")
    # 能力上の合否に数えない
    assert store.capability_outcomes("our-free-model/b-free") == []
    ex = promotion.rate_limited_exclude(store, "our-free-model/b-free", now="2026-11-05T12:30:00.000Z", exclude_min=60)
    assert ex["exclude"] is True and ex["reason"] == "rate_limit"
    # exclude_rate_limit_min 後は再評価
    ex2 = promotion.rate_limited_exclude(store, "our-free-model/b-free", now="2026-11-05T13:30:00.000Z", exclude_min=60)
    assert ex2["exclude"] is False


def test_free_recent_failure_and_success_rate(tmp_path):
    store = resources_store(tmp_path)
    seed_models(store)
    store.record_outcome("our-free-model/b-free", outcome="fail", counts_as_failure="yes", verify_rc=1,
                         now="2026-11-05T12:00:00.000Z")
    store.record_outcome("our-free-model/b-free", outcome="pass", counts_as_failure="no", verify_rc=0,
                         now="2026-11-05T12:01:00.000Z")
    r = promotion.recently_failed(store, "our-free-model/b-free", now="2026-11-05T12:05:00.000Z", recent_min=60)
    # last is pass, so no recent failure exclude
    assert r["exclude"] is False
    # success 1/2 is below 0.7 and trials>=3? only 2 trials
    s = promotion.free_success(store, "our-free-model/b-free")
    assert s["trials"] == 2 and s["success_rate"] == 0.5
    ex = promotion.should_exclude_for_success(store, "our-free-model/b-free", min_success=0.7, min_runs=2)
    assert ex["exclude"] is True


def test_unpromoted_free_not_assigned_impl_via_promotion_trial(tmp_path):
    # 未昇格の無料モデルに昇格試行を理由に実装を割り当てない（R-N26）
    store = resources_store(tmp_path)
    models = seed_models(store)
    a_free = next(m for m in models if m["id"] == "our-free-model/a-free")
    assert a_free["level"] == "L1"
    # impl の最低段階は L3。L1 の free は選べない
    import importlib
    sel = importlib.import_module("plugins.resource.worker-pool.selection")
    r = sel.select(models, role="impl", min_level="L3")
    assert r["selected"] != "our-free-model/a-free"
