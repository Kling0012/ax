"""T-113 計測と適用（S-8.12 の 14）。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib
_probe = importlib.import_module("plugins.resource.worker-pool.probe")
_jobs_api = importlib.import_module("plugins.resource.worker-pool.jobs")
from tests.b6a.fixtures import jobs_store, resources_store, seed_models
from core.resources.core_auth import CoreAuth
from axcommon import creds


def test_same_prefix_two_runs_within_gap_records_ratio(tmp_path):
    assert _probe.probe_prefix_id() == _probe.probe_prefix_id()
    assert _probe.within_gap("2026-11-05T12:00:00.000Z", "2026-11-05T12:01:00.000Z", gap_s=120) is True
    assert _probe.within_gap("2026-11-05T12:00:00.000Z", "2026-11-05T12:03:00.000Z", gap_s=120) is False


def test_on_off_unknown_by_floor(tmp_path):
    assert _probe.decide(ratio=0.6, floor=0.5) == "on"
    assert _probe.decide(ratio=0.4, floor=0.5) == "off"
    assert _probe.decide(ratio=None, floor=0.5) == "unknown"


def test_internal_pin_model_only(tmp_path):
    store = jobs_store(tmp_path)
    job = _jobs_api.submit_internal(store, {"role": "summary", "prompt": "x", "sensitivity": "public"}, pin_model="m-fixed")
    assert job["pin_model"] == "m-fixed"
    try:
        _jobs_api.validate_job_body({"role": "summary", "prompt": "x", "sensitivity": "public", "pin_model": "m"})
        raise AssertionError("pin_model を拒否しなかった")
    except _jobs_api.ValidationError as e:
        assert str(e) == "pin_model_not_allowed"
    try:
        _jobs_api.validate_job_body({"role": "summary", "prompt": "x", "sensitivity": "public", "session_id": "sid"})
        raise AssertionError("session_id を拒否しなかった")
    except _jobs_api.ValidationError:
        pass


def test_prefix_cache_write_only_worker_pool_credential(tmp_path, fake_clock):
    store = resources_store(tmp_path)
    seed_models(store)
    tok = "a" * 64
    table = creds.TokenTable({"worker-pool": creds.sha256_hex(tok)})
    auth = CoreAuth(table)
    cred, result = auth.check(f"Bearer {tok}", "gate-token")
    assert result == "ok" and cred == "worker-pool"
    cred2, result2 = auth.check("Bearer " + "b" * 64, "gate-token")
    assert cred2 is None and result2 == "unauthenticated"
    out = store.set_prefix_cache("zcode/GLM-5.3", "on", ratio=0.6, measured_at="2026-11-05T12:00:00.000Z")
    assert out["prefix_cache"] == "on"
