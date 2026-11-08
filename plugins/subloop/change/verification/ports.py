"""Adapters to b6b launcher and b14b1 env jobs. All transport is caller-injected.

Attestation is trusted host-side measurement, never PR-controlled output.
Collectors must enforce timeout_s, capture complete logs and normalized diagnostics.
They must return executed=False for connection failures, never synthesize success.
"""
from __future__ import annotations

import math

from .policy import GateError


class B6bRunner:
    def __init__(self, launcher, *, attest, collect):
        self.launcher = launcher
        self.attest = attest
        self.collect = collect

    def prepare(self, invocation):
        return self.attest(invocation)

    def execute(self, invocation, prepared):
        if invocation["environment"] not in ("verify-node", "verify-python", "verify-dotnet", "verify-ax-ci"):
            raise GateError("verify_unavailable")
        # The trusted preparer materializes /work/repo at subject_sha and preserves
        # dependencies within workdir_id; separate attempts have separate workdirs.
        req = {"job_id": invocation["job_id"], "runner": invocation["environment"],
               "allow_hosts": invocation["allow_hosts"], "budget_min": math.ceil(invocation["timeout_s"] / 60),
               "workdir_id": invocation["workdir_id"], "ro_mounts": invocation["ro_mounts"],
               "verify": "cd /work/repo && " + invocation["command"]}
        self.launcher.job_start(req)
        return self.collect(invocation, lambda: self.launcher.job_status(invocation["job_id"]))


class EnvironmentRunner:
    def __init__(self, *, submit, status, attest, collect):
        # submit/status are bound to the registered environment, through b14b1.
        self.submit = submit
        self.status = status
        self.attest = attest
        self.collect = collect

    def prepare(self, invocation):
        return self.attest(invocation)

    def execute(self, invocation, prepared):
        if invocation["environment"] != "env:example-local:hibi-server-e2e" or invocation["stage"] != "verify":
            raise GateError("verify_unavailable")
        if prepared.get("env_id") != "example-local" or not isinstance(prepared.get("worktree"), str) or not prepared["worktree"]:
            raise GateError("verify_unavailable")
        req = {"job_id": invocation["job_id"], "requires": ["docker"], "sensitivity": "private",
               "entry": {"kind": "entrypoint", "name": "hibi-server-e2e", "args": {"worktree": prepared["worktree"]}},
               "inputs": [{"ref": f"git:{invocation['repo']}@{invocation['subject_sha']}"}],
               "outputs": ["e2e.log"], "secrets": [], "budget_min": 40, "stall_min": 40,
               "irreversible": False, "user_requested": False, "scope": "one", "idempotent": False,
               "idempotency_key": invocation["job_id"]}
        # No caller-supplied shell command is sent to this fixed entrypoint.
        reply = self.submit(req)
        if isinstance(reply, dict) and reply.get("env_id") not in (None, "example-local"):
            raise GateError("environment_unconfirmed")
        return self.collect(invocation, lambda: self.status(invocation["job_id"]))


class RoutedRunner:
    def __init__(self, local, environment):
        self.local = local
        self.environment = environment

    def _port(self, invocation):
        return self.environment if invocation["environment"].startswith("env:") else self.local

    def prepare(self, invocation):
        return self._port(invocation).prepare(invocation)

    def execute(self, invocation, prepared):
        return self._port(invocation).execute(invocation, prepared)
