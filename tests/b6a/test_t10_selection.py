"""T-10 選び方（S-8.4）。

定額（flat）の候補は単価がなくても保留にならず、tier の順で選ばれる。
従量課金で単価が未登録の候補だけが外れる。Jev が適合と判定した安全条件を
満たす候補から最安を選び、同額は固定順で選ぶ。Jev 不正時も適格な候補から最安を選ぶ。
予備役は例外として Sonnet 5.5 だけを使う。test_design の最後の手段にも Sonnet 5.5 を使う。GPT は指定された
2 条件のいずれかを要する。停滞・時間切れ・実行主体の消失を区別し、quota を
能力の不合格に数えない。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib; _m = importlib.import_module("plugins.resource.worker-pool.selection"); Knobs, select, slots_for = _m.Knobs, _m.select, _m.slots_for
from tests.b6a.fixtures import default_models, resources_store, seed_models
from tests.fakes.b6a.jev_fake import FakeJev
from core.resources import jev as jev_mod


def _models(tmp_path):
    store = resources_store(tmp_path)
    return seed_models(store)


def test_flat_candidates_need_no_price_and_follow_tier_order(tmp_path):
    models = [m for m in _models(tmp_path) if m["billing"] == "flat"]
    # 単価が無い GLM でも、保留にしない
    r = select(models, role="impl", min_level="L3")
    assert r["selected"] is not None and r["reason"] == "cheapest_eligible"
    assert r["selected"] == "zhipu-cp/glm-5.3-flash"  # tier flash が standard より前


def test_metered_unpriced_is_excluded_and_recorded(tmp_path):
    models = _models(tmp_path)
    metered_only = [m for m in models if m["id"] == "mistral/metered-m"]
    r = select(metered_only, role="impl", min_level="L3")
    assert r["selected"] is None and r["reason"] == "parked_metered_unpriced"
    assert any(x["id"] == "mistral/metered-m" and x["reason"] == "metered_unpriced" for x in r["excluded"])


def test_flat_exists_means_no_parked(tmp_path):
    models = _models(tmp_path)
    r = select(models, role="impl", min_level="L3")
    assert r["selected"] is not None  # 定額の候補があれば保留にしない（S-8.4 の 3）


def test_metered_priced_is_after_flat(tmp_path):
    models = _models(tmp_path)
    r = select(models, role="impl", min_level="L3")
    # metered より flat が先
    assert r["selected"] != "rog/metered-x"


def test_jev_filters_to_suitable_then_cheapest(tmp_path, fake_clock):
    models = _models(tmp_path)
    fake = FakeJev()
    fake.set("zhipu-cp/glm-5.3-flash", 0.9)
    fake.set("zcode/GLM-5.3", 0.9)
    # 適合と判定した候補から最安を選ぶ
    r = select(models, role="impl", min_level="L3", jev_scores={"zhipu-cp/glm-5.3-flash": 0.9, "zcode/GLM-5.3": 0.9},
               jev_available=True)
    assert r["selected"] == "zhipu-cp/glm-5.3-flash"
    # 適合外の候補は選ばない
    fake2 = FakeJev()
    fake2.set("our-free-model/a-free", 0.1)
    res = jev_mod.call(state={"model_id": "our-free-model/a-free"}, questions={"suitable": "適合するか"},
                       transport=fake2.transport, key="fake")
    assert res["ok"] and jev_mod.verdict(res["answers"]["suitable"]) == "no"


def test_jev_unavailable_falls_back_to_cheapest_eligible(tmp_path):
    models = _models(tmp_path)
    fake = FakeJev().fail_http(503)
    res = jev_mod.call(state={"model_id": "zcode/GLM-5.3"}, questions={"suitable": "適合するか"},
                       transport=fake.transport, key="fake")
    assert not res["ok"]
    r = select(models, role="impl", min_level="L3", jev_available=False)
    assert r["selected"] is not None


def test_jev_timeout_is_unavailable(tmp_path, fake_clock):
    fake = FakeJev().timeout()
    res = jev_mod.call(state={"model_id": "zcode/GLM-5.3"}, questions={"suitable": "適合するか"},
                       transport=fake.transport, key="fake")
    assert not res["ok"] and "transport" in res["reason"]


def test_backup_manager_picks_only_sonnet55(tmp_path):
    models = _models(tmp_path)
    r = select(models, role="backup_manager", min_level="L4")
    assert r["selected"] == "claude/claude-sonnet-5-5"
    excluded_ids = {x["id"] for x in r["excluded"]}
    assert "claude/claude-opus-4-8" in excluded_ids  # Opus は自動経路に入れない


def test_manager_excludes_gpt_and_claude_even_when_other_restricted_uses_are_allowed(tmp_path):
    models = _models(tmp_path)
    r = select(models, role="manager", min_level="L1", cheap_failures=2, hard_use=True)
    forbidden = {"codex/gpt-6.1-sol", "codex/gpt-6.1-terra", "claude/claude-sonnet-5-5", "claude/claude-opus-4-8"}
    assert not forbidden & set(r["candidates"])
    assert r["selected"] not in forbidden
    excluded = {x["id"]: x["reason"] for x in r["excluded"]}
    assert {mid: excluded[mid] for mid in forbidden} == {mid: "manager_gpt_claude_forbidden" for mid in forbidden}


def test_gpt_needs_second_condition(tmp_path):
    models = _models(tmp_path)
    # 2 条件のいずれかを要する（S-8.4 の 2）
    r = select(models, role="impl_hard", min_level="L4", cheap_failures=0, hard_use=False)
    assert r["selected"] != "codex/gpt-6.1-sol"
    r2 = select(models, role="impl_hard", min_level="L4", cheap_failures=2, hard_use=False)
    assert r2["selected"] is not None and r2["candidates"]
    assert any(x in r2["candidates"] for x in ("codex/gpt-6.1-sol", "codex/gpt-6.1-terra"))
    r3 = select(models, role="impl_hard", min_level="L4", cheap_failures=0, hard_use=True)
    assert r3["selected"] is not None


def test_sensitivity_tiers_are_respected(tmp_path):
    models = _models(tmp_path)
    r = select(models, role="impl", min_level="L3", sensitivity="sensitive")
    # sensitive は tier が standard 以上の登録済みモデルだけ（S-15.1）
    sel = next(m for m in models if m["id"] == r["selected"])
    assert sel["tier"] in ("standard", "last_resort")


def test_needs_vision_filters(tmp_path):
    models = _models(tmp_path)
    r = select(models, role="summary", min_level="L1", needs_vision=True)
    sel = next(m for m in models if m["id"] == r["selected"])
    assert sel.get("vision") is True


def test_second_opinion_excludes_same_family(tmp_path):
    models = _models(tmp_path)
    r = select(models, role="second_opinion", min_level="L1", exclude_ids={"our-free-model/a-free"})
    assert r["selected"] != "our-free-model/a-free"
    assert any(x["id"] == "our-free-model/a-free" for x in r["excluded"])


def test_quota_is_not_a_capability_failure(tmp_path):
    store = resources_store(tmp_path)
    seed_models(store)
    import importlib as _il; _sm = _il.import_module("plugins.resource.worker-pool.supervise"); counts_as_failure, error_class_of = _sm.counts_as_failure, _sm.error_class_of

    assert error_class_of(api_error_status=429) == "rate_limit"
    assert counts_as_failure("rate_limit") == "hold"


def test_stall_and_timeout_and_lost_are_distinct(tmp_path):
    import importlib as _il2; classify_state = _il2.import_module("plugins.resource.worker-pool.supervise").classify_state

    assert classify_state(state="running", wall_s=61*60, budget_min=60, container_exists=True, caller_alive=True)["state"] == "timed_out"
    assert classify_state(state="running", idle_s=16*60, stall_min=15, container_exists=True, caller_alive=True)["state"] == "stalled"
    assert classify_state(state="running", container_exists=False, caller_alive=True)["state"] == "lost"


def test_tie_breaks_by_role_order_then_id(tmp_path):
    unit = [
        {"id": "a/free", "kind": "model", "family": "free", "tier": "free", "billing": "flat", "level": "L1", "status": "active"},
        {"id": "z/free", "kind": "model", "family": "free", "tier": "free", "billing": "flat", "level": "L1", "status": "active"},
    ]
    # 同額なら S-8.3 の順、同順位なら ID 昇順（S-8.4 の 3）
    r = select(unit, role="summary", min_level="L1")
    assert r["selected"] == "a/free"


def test_slots_count_launching_submitted_running_only(tmp_path):
    models = _models(tmp_path)
    jobs = [
        {"state": "running", "model": "zcode/GLM-5.3"},
        {"state": "launching", "model": "zcode/GLM-5.3"},
        {"state": "queued", "model": "zcode/GLM-5.3"},
        {"state": "ok", "model": "zcode/GLM-5.3"},
        {"state": "submitted", "model": "zhipu-cp/glm-5.3-flash"},
    ]
    s = slots_for(models, jobs, Knobs(family_cap={"glm": 3, "free": 5}, model_cap={"zcode/GLM-5.3": 2}))
    assert s["by_model"]["zcode/GLM-5.3"] == 2 and s["by_model"]["zhipu-cp/glm-5.3-flash"] == 1
    assert s["by_family"]["glm"] == 2
    # free の実効上限は min（系統の調整値、active な無料モデルの個別上限の合計）（S-8.7）
    assert s["family_cap"]["free"] == 2  # cap 1 が 2 件


@pytest.mark.parametrize('role', ['summary', 'impl', 'impl_hard', 'review_final', None])
@pytest.mark.parametrize('hard_use,cheap_failures', [(False, 0), (True, 0), (False, 2)])
def test_claude_excluded_for_other_roles_even_with_gpt_exceptions(tmp_path, role, hard_use, cheap_failures):
    models = [m for m in _models(tmp_path) if m['family'] == 'claude']
    result = select(models, role=role, hard_use=hard_use, cheap_failures=cheap_failures)
    assert result['selected'] is None


@pytest.mark.parametrize('role', ['backup_manager', 'test_design'])
def test_sonnet_still_obeys_safety_conditions(tmp_path, role):
    sonnet = next(m for m in _models(tmp_path) if m['id'] == _m.BACKUP_MODEL_ID)
    assert select([sonnet], role=role, min_level='L4')['selected'] == sonnet['id']
    assert select([{**sonnet, 'status': 'disabled'}], role=role)['selected'] is None
    assert select([sonnet], role=role, min_level='L5')['selected'] is None
    assert select([{**sonnet, 'tier': 'free'}], role=role, sensitivity='sensitive')['selected'] is None
    assert select([{**sonnet, 'vision': False}], role=role, needs_vision=True)['selected'] is None
    assert select([sonnet], role=role, quota_exhausted_families={'claude'})['selected'] is None
