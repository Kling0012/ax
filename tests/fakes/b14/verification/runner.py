"""Offline runner/attestation fixture: no subprocesses, sockets or services."""
from copy import deepcopy


class FakeRunner:
    def __init__(self, *, prepare_override=None, result_override=None):
        self.prepared = []
        self.executed = []
        self.prepare_override = prepare_override
        self.result_override = result_override

    def prepare(self, invocation):
        self.prepared.append(deepcopy(invocation))
        result = {"available": True, "sha": invocation["subject_sha"], "job_id": invocation["job_id"],
                  "environment": invocation["environment"], "entry_verified": True, "env_id": "example-local",
                  "isolation_verified": True, "home_absent": True, "credentials_absent": True,
                  "host_config_absent": True, "network_enforced": True,
                  "network_mode": "allowlist" if invocation["stage"] == "dependency" else "none",
                  "allow_hosts": invocation["allow_hosts"], "privacy_patterns_staged": True,
                  "worktree": "/work/" + invocation["workdir_id"] + "/repo"}
        if callable(self.prepare_override):
            self.prepare_override(invocation, result)
        elif self.prepare_override:
            result.update(self.prepare_override)
        return result

    def execute(self, invocation, prepared):
        self.executed.append(deepcopy(invocation))
        result = {"job_id": invocation["job_id"], "sha": invocation["subject_sha"], "executed": True,
                  "started_at": "2026-11-07T00:00:00Z", "ended_at": "2026-11-07T00:00:01Z",
                  "exit_code": 0, "timed_out": False, "complete": True, "output_truncated": False,
                  "output_tail": "2 passed", "test_names": ["test_a", "test_b"], "failure_evidence": {}}
        if callable(self.result_override):
            self.result_override(invocation, result)
        elif self.result_override:
            result.update(self.result_override)
        return result
