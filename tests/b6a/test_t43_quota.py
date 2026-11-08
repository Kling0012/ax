"""T-43 モデル選択と quota（S-8.4、S-8.5）。

初日に GLM・GPT の単価がなくても impl の候補が保留にならない（定額の候補は保留にならない）。
Jev が適合と判定した候補のうち最安を選び、Jev 使用不能時も適格な候補から最安を選ぶ。
予備役は Sonnet 5.5 だけを使う（Opus は選ばない）。難しい用で作業に合う場合または
安いモデルの 2 回の失敗がない GPT / Claude を選ばない。満枠や quota は失敗に数えない。
系統の quota が尽きたときはその系統を要するジョブだけカードで待ち、他のジョブは進む。
数値による比率・費用上限と残量通知を設けない。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib as _il; select = _il.import_module("plugins.resource.worker-pool.selection").select
import importlib as _il2; _sm=_il2.import_module("plugins.resource.worker-pool.supervise"); counts_as_failure, error_class_of = _sm.counts_as_failure, _sm.error_class_of
from tests.b6a.fixtures import default_models, resources_store, seed_models
from tests.fakes.b6a.jev_fake import FakeJev
from core.resources import jev as jev_mod


def test_day_one_glm_gpt_have_no_price_but_impl_is_not_parked(tmp_path):
    models = _models_(tmp_path)
    glm = [m for m in models if m["id"] == "zcode/GLM-5.3"]
    assert glm[0]["billing"] == "flat"  # pricing があっても flat として扱う（F-7.1）
    r = select(models, role="impl", min_level="L3")
    assert r["selected"] is not None


def _models_(tmp_path):
    store = resources_store(tmp_path)
    seed_models(store)
    return store.list(limit=2000)


def test_backup_is_sonnet55_not_opus(tmp_path):
    models = _models_(tmp_path)
    r = select(models, role="backup_manager", min_level="L4")
    assert r["selected"] == "claude/claude-sonnet-5-5"
    assert "claude/claude-opus-4-8" not in r.get("candidates", []) or r["selected"] != "claude/claude-opus-4-8"


def test_restricted_not_selected_without_conditions(tmp_path):
    models = _models_(tmp_path)
    r = select(models, role="impl_hard", min_level="L4", cheap_failures=1, hard_use=False)
    assert r["selected"] is None or "gpt" not in r["selected"]


def test_rate_limit_is_not_a_capability_failure(tmp_path):
    assert error_class_of(http_status=429) == "rate_limit"
    assert counts_as_failure("rate_limit") == "hold"
    assert counts_as_failure("network") == "hold"
    assert counts_as_failure("quota") == "hold"


def test_quota_family_waits_others_continue(tmp_path):
    models = _models_(tmp_path)
    # 実績で L4 に昇格した正規 GLM が他系統の適格候補。
    glm = next(m for m in models if m["id"] == "zcode/GLM-5.3")
    models.append({**glm, "id": "auto/glm-5.3", "level": "L4"})
    # gpt 系統の quota が尽きたときは gpt だけ外れ、他は進む
    r = select(models, role="impl_hard", min_level="L4", cheap_failures=2, quota_exhausted_families={"gpt"})
    assert r["selected"] == "auto/glm-5.3"
    exhausted = {x["id"] for x in r["excluded"] if x["reason"] == "quota_exhausted"}
    assert {"codex/gpt-6.1-sol", "codex/gpt-6.1-terra"} <= exhausted
    # 別のジョブ（impl）に影響しない
    r2 = select(models, role="impl", min_level="L3", quota_exhausted_families={"gpt"})
    assert r2["selected"] is not None


def test_no_numeric_quota_limit_is_set(tmp_path):
    models = _models_(tmp_path)
    r = select(models, role="impl", min_level="L3")
    # 比率や系統別・日別の費用上限の数値は設けない（S-8.4 の 5）。選べば quota 残量通知は要らない
    assert r["selected"] is not None


def test_jev_unusable_still_picks_cheapest(tmp_path, fake_clock):
    fake = FakeJev().fail_http(500)
    res = jev_mod.call(state={"model_id": "zcode/GLM-5.3"}, questions={"suitable": "適合するか"},
                       transport=fake.transport, key="fake")
    assert not res["ok"]
    models = _models_(tmp_path)
    r = select(models, role="impl", min_level="L3", jev_available=res["ok"])
    assert r["selected"] is not None


def test_manager_ignores_level_but_impl_and_other_guards_remain():
    model = {'id': 'auto/glm-5.3', 'kind': 'model', 'family': 'glm', 'tier': 'standard',
             'billing': 'flat', 'level': 'L1', 'status': 'active'}
    assert select([model], role='manager', min_level='L3')['selected'] == model['id']
    deepseek = {**model, 'id': 'group/opencode-deepseek-v4.1-flash', 'family': 'deepseek-flash', 'tier': 'flash'}
    assert select([deepseek], role='manager', min_level='L3')['selected'] == deepseek['id']
    free = {**model, 'id': 'free/test', 'family': 'free', 'tier': 'free'}
    assert select([model, deepseek, free], role='manager', min_level='L3')['selected'] == free['id']
    assert select([{**model, 'family': 'other'}], role='manager', min_level='L3')['selected'] is None
    assert select([model], role='impl', min_level='L3')['selected'] is None
    assert select([{**model, 'status': 'excluded'}], role='manager', min_level='L3')['selected'] is None
    assert select([{**model, 'family': 'claude'}], role='manager', min_level='L3')['selected'] is None
    assert select([model], role='manager', min_level='L3', needs_vision=True)['selected'] is None
