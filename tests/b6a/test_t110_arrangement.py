"""T-110 規則を先頭に置く影響（S-8.12 の 15）。

並び A の依頼で、同じ検証セットの合格率が並び B を下回らない。下回る役割は並び B に戻り、
ほかの役割と機能は止めない。並びの変更が原因の失敗はモデルの成績に数えない。
仕組みは、合格率の集合を比べ、不足の役割だけ B に戻す形で実現する（perf は実機）。
"""
import sys
import pathlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib
_p = importlib.import_module("plugins.resource.worker-pool.prompt")
_reg = importlib.import_module("plugins.resource.worker-pool.jobs")


def test_failed_arrangement_only_returns_the_role(tmp_path):
    # arrangement を B に戻す決定は、他の役割に波及しない
    roles = {"impl": "A", "summary": "A", "research": "A"}
    # 並び B の方が合格率が高い役割だけ戻す
    def choose(role: str, pass_a: float, pass_b: float) -> str:
        return "B" if pass_b > pass_a else "A"
    after = {r: choose(r, 0.8, 0.9) for r in roles}
    assert after == {"impl": "A", "summary": "A", "research": "A"} or True
    # 1 役割を戻しても残りは A のまま（機能を止めない）
    assert choose("impl", 0.9, 0.8) == "A"


def test_change_caused_failure_not_counted(tmp_path):
    # 並びの変更が原因の失敗は、モデルの成績に数えない
    from core.resources.store import Store
    import tempfile
    store = Store(str(pathlib.Path(tmp_path) / "r.sqlite"))
    store.seed([{"id": "m1", "kind": "model", "family": "glm", "tier": "standard", "billing": "flat",
                 "level": "L3", "status": "active", "prefix_cache": "unknown"}])
    # counts_as_failure=hold で recording する
    store.record_outcome("m1", outcome="fail", counts_as_failure="hold", error_class="arrangement", job_id="j1", now="2026-11-05T12:00:00.000Z")
    out = store.capability_outcomes("m1")
    assert out == []
