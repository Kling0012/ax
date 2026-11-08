"""T-44 下位候補（S-8.4、S-8.6、R-N20、R-N21）。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib as _il
_q = _il.import_module("plugins.resource.worker-pool.queueing")
lower_fallback_allowed = _q.lower_fallback_allowed
attribute_failure_to_candidate = _q.attribute_failure_to_candidate
should_queue = _q.should_queue


def test_fallback_when_permit_wait_verify_ready(tmp_path):
    ok, reason = lower_fallback_allowed(enabled=True, waited_s=300, wait_required_s=300, has_verify=True, safety_ok=True)
    assert ok and reason == "lower_fallback"


def test_no_verify_waits(tmp_path):
    ok, reason = lower_fallback_allowed(enabled=True, waited_s=300, wait_required_s=300, has_verify=False, safety_ok=True)
    assert not ok and reason == "no_verify"


def test_wait_not_met_waits(tmp_path):
    ok, reason = lower_fallback_allowed(enabled=True, waited_s=10, wait_required_s=300, has_verify=True, safety_ok=True)
    assert not ok and reason == "lower_wait_not_met"


def test_other_models_failure_is_not_the_candidates_score(tmp_path):
    assert attribute_failure_to_candidate(trial_model="some-other-model", candidate="candidate-1") is False
    assert attribute_failure_to_candidate(trial_model="candidate-1", candidate="candidate-1") is True


def test_role_queue_max_skips(tmp_path):
    queue = [{"role": "impl"} for _ in range(5)]
    ok, reason = should_queue(queue, "impl", role_queue_max=5)
    assert not ok and reason == "queue_full"
