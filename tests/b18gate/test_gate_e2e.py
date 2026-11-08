"""Exercise the public sandbox service with fake providers and slots."""
import hashlib

import pytest

from core.resources.envs.sandbox.free_guard import FreeMeasurement
from core.resources.envs.sandbox.gate import GateDenied, GateService
from core.resources.envs.sandbox.keys import DuplicateKey, check_distinct
from core.resources.envs.sandbox.probe_common import ProviderState, apply_gate_key


class FakeProvider:
    def __init__(self):
        self.key = b"worker-secret-do-not-record"
        self.measurement = FreeMeasurement(1, 1, 0, 80, 100, 10)
        self.slots = set()

    def read_worker_key(self):
        return self.key

    def measure(self):
        return self.measurement

    def reserve(self, env, job):
        if env in self.slots:
            return False
        self.slots.add(env)
        return True

    def release(self, env, job):
        self.slots.remove(env)


@pytest.fixture
def setup_gate():
    provider = FakeProvider()
    env = ProviderState("fake")
    credential = "1" * 64
    service = GateService(auth_digest=hashlib.sha256(credential.encode()).hexdigest(), state=env,
                          capabilities={"web-gui"}, read_worker_key=provider.read_worker_key,
                          measure=provider.measure, reserve=provider.reserve, release=provider.release,
                          clock=lambda: 1000)
    request = {"job_id": "job-1", "role": "research", "sensitivity": "public", "tools": ["web-gui"], "budget_min": 20}
    return provider, env, service, "Bearer " + credential, request


def make_ready(env):
    env.record_ax_probe(True)
    env.complete_j_trial(success=True, trial_for="fake")
    apply_gate_key(env, "ok", 1000)
    env.complete_g_trial(success=True, trial_for="fake", now=1000, max_age_s=300)


def test_missing_worker_key_only_excludes_gate(setup_gate):
    provider, env, service, auth, request = setup_gate
    make_ready(env)
    provider.key = None
    with pytest.raises(GateDenied, match="worker_key_missing"):
        service.issue(auth, request)
    assert (env.state, env.gate_state, env.gate_state_reason) == ("available", "excluded", "worker_key_unavailable")
    assert not provider.slots


def test_trials_require_correct_order_and_provider(setup_gate):
    _, env, _, _, _ = setup_gate
    apply_gate_key(env, "ok", 1000)
    with pytest.raises(ValueError):
        env.complete_g_trial(success=True, trial_for="fake", now=1000, max_age_s=300)
    with pytest.raises(ValueError):
        env.complete_j_trial(success=True, trial_for="fake")
    env.record_ax_probe(True)
    with pytest.raises(ValueError):
        env.complete_j_trial(success=True, trial_for="other")
    env.complete_j_trial(success=True, trial_for="fake")
    with pytest.raises(ValueError):
        env.complete_g_trial(success=True, trial_for="other", now=1000, max_age_s=300)
    env.complete_g_trial(success=True, trial_for="fake", now=1000, max_age_s=300)
    assert env.gate_state == "available"


def test_duplicate_key_rejected_and_audit_contains_only_slot_and_match():
    events = []
    secret = b"secret-key-value"
    with pytest.raises(DuplicateKey):
        check_distinct("worker", secret, {"ax": secret, "other": b"other-secret"}, events.append)
    assert events == [{"slot": "ax", "match": True}, {"slot": "other", "match": False}]
    assert secret.decode() not in str(events)


@pytest.mark.parametrize("measurement,reason", [
    (FreeMeasurement(1, 1, 0, 10, 100, 0), "quota"),
    (FreeMeasurement(1, 1, 0, 100, 25, 10), "run_budget_exhausted"),
    (None, "free_unverified"),
    (FreeMeasurement(2, 1, 0, 100, 100, 0), "machines_over_limit"),
    (FreeMeasurement(1, 1, float('nan'), 100, 100, 0), "free_unverified"),
])
def test_free_limits_fail_closed(setup_gate, measurement, reason):
    provider, env, service, auth, request = setup_gate
    make_ready(env)
    provider.measurement = measurement
    with pytest.raises(GateDenied, match=reason):
        service.issue(auth, request)
    assert not provider.slots


def test_success_issue_revoke_and_no_secret_in_representation(setup_gate):
    provider, env, service, auth, request = setup_gate
    make_ready(env)
    issued = service.issue(auth, request)
    assert service.validate(issued.token)
    assert issued.token not in repr(issued)
    assert provider.key.decode() not in repr(issued)
    assert service.revoke(issued.token_id)
    assert not service.revoke(issued.token_id)
    assert not service.validate(issued.token)
    assert not provider.slots
