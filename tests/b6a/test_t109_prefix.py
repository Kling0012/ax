"""T-109 固定部分の並び（S-8.12）。

同じ役割・プロジェクト・教訓で 2 つの作業を作ると、(1)〜(3) のバイト列と prefix_id が同じになる。
時刻、ジョブ ID、試行番号、時間予算は (4) だけに入る。教訓の集合が違えば prefix_id が変わる。
作業ディレクトリが /work で、同じ役割のツール一覧と順番が同じ。
prefix_cache が off と unknown のモデルは並び B になる。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib
_p = importlib.import_module("plugins.resource.worker-pool.prompt")


def test_same_role_project_lessons_same_prefix_id(tmp_path):
    p1 = _p.PromptParts(standing="r1\n", project="proj\n", lessons=(("l1", "lesson-1"),), job=_p.job_block(prompt="do a", job_id="job-1", attempt_no=1))
    p2 = _p.PromptParts(standing="r1\n", project="proj\n", lessons=(("l1", "lesson-1"),), job=_p.job_block(prompt="do b", job_id="job-2", attempt_no=2))
    assert _p.prefix_id(p1) == _p.prefix_id(p2)
    assert _p.prefix_blocks(p1) == _p.prefix_blocks(p2)


def test_time_job_attempt_budget_are_only_in_part4(tmp_path):
    p1 = _p.PromptParts(standing="r1\n", lessons=(("l1", "x"),), job="prompt\n[ax job {\"job_id\": \"job-1\", \"attempt\": 1}]")
    p2 = _p.PromptParts(standing="r1\n", lessons=(("l1", "x"),), job="prompt\n[ax job {\"job_id\": \"job-2\", \"attempt\": 2}]")
    assert _p.prefix_id(p1) == _p.prefix_id(p2)
    assert p1.job != p2.job


def test_different_lessons_change_prefix_id(tmp_path):
    p1 = _p.PromptParts(standing="r1\n", lessons=(("l1", "a"),))
    p2 = _p.PromptParts(standing="r1\n", lessons=(("l2", "a"),))
    assert _p.prefix_id(p1) != _p.prefix_id(p2)


def test_on_is_a_off_unknown_is_b(tmp_path):
    assert _p.arrangement_for("on") == "A"
    assert _p.arrangement_for("off") == "B"
    assert _p.arrangement_for("unknown") == "B"
    parts = _p.PromptParts(standing="S", job="P")
    assert _p.build(parts, arrangement="A").startswith("S")
    assert _p.build(parts, arrangement="B").startswith("P") and "S" in _p.build(parts, arrangement="B")
