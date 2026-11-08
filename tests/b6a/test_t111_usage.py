"""T-111 使用量の記録（S-8.12）。

各試行に usage と初回ターンの値が入る。報告のないモデルは不明で、0 にならない。
取得に失敗しても作業は成否を保ち、他の機能は止まらない。
HQ で役割・モデル別の初回の読み込み比率を見られる。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib
_u = importlib.import_module("plugins.resource.worker-pool.usage")


def test_usage_and_first_turn_recorded(tmp_path):
    doc = {"usage": {"input_tokens": 100, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 0, "output_tokens": 20}, "session_id": "sid-1"}
    lu = {"first_input": 24, "first_cache_read": 12, "last_context": 80}
    u = _u.usage_of(doc, lu)
    assert u["input"] == 100 and u["first_input"] == 24 and u["first_cache_read"] == 12
    assert u["first_cache_ratio"] == 0.5


def test_unreported_is_unknown_not_zero(tmp_path):
    u = _u.usage_of(None, None)
    assert u["input"] is None and u["unknown"]
    # 0 と区別する
    assert u["input"] is None


def test_failure_does_not_affect_job_outcome(tmp_path):
    # 取得に失敗してもジョブの成否は保つ
    u = _u.usage_of({"result": "ok"}, {"first_input": 10})
    assert u["first_input"] == 10
    # container log が無くてもこの関数は落ちない
    assert _u.parse_container_log("") is None


def test_role_model_first_cache_ratio_aggregation(tmp_path):
    events = [
        {"data": {"role": "summary", "model": "m1", "runner": "agent-cli", "usage": {"first_cache_ratio": 0.6}}},
        {"data": {"role": "summary", "model": "m1", "runner": "agent-cli", "usage": {"first_cache_ratio": 0.8}}},
        {"data": {"role": "summary", "model": "m2", "runner": "agent-cli", "usage": {"first_cache_ratio": 0.2}}},
    ]
    agg = _u.aggregate_by_role_model(events)
    m1 = next(a for a in agg if a["model"] == "m1")
    assert m1["n"] == 2 and abs(m1["median_ratio"] - 0.7) < 1e-9
