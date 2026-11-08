"""Ephemeral cloud sandbox adapter and environment executor."""
from __future__ import annotations

import copy
import hashlib
import logging
import threading
from typing import Any, Mapping

from axcommon.redact import from_environment
from core.resources.envs.adapters.base import AdapterError, BaseEnvironmentAdapter
from core.resources.envs.sandbox.instabox import InstaboxCancelled, InstaboxTimeout

log = logging.getLogger(__name__)
_BOXES_LOCK = threading.Lock()
_BOXES = {}



def _scan(value: Any) -> None:
    redactor = from_environment()
    if not redactor.index_path or not redactor.pii_list_path or not redactor.index_ok or not redactor.pii_list_ok:
        raise AdapterError("outbound scanner unavailable")

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if redactor.contains_secret(node):
                raise AdapterError("outbound content rejected")
        elif isinstance(node, Mapping):
            for key, item in node.items():
                walk(key)
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif node is not None and not isinstance(node, (bool, int, float)):
            raise AdapterError("unsupported outbound content")
    walk(value)


class CloudSandboxExecutor:
    """Synchronous jobs; cancellation can be requested by another thread."""
    def __init__(self, driver, *, remote_id: str, scan_outbound=None, scan_inbound=None, state_store=None):
        self.driver = driver
        self.remote_id = remote_id
        self.scan_outbound = scan_outbound
        self.scan_inbound = scan_inbound
        self.state_store = state_store
        self._lock = threading.RLock()
        self._jobs = {}
        self._keys = {}
        # All adapters for the same configured box share its in-process guard.
        with _BOXES_LOCK:
            self._box = _BOXES.setdefault((driver.executable, remote_id),
                                          {"lock": threading.RLock(), "active": False, "uncertain": False})

    def execute(self, *, connection, command, payload=None):
        if connection.kind != "cloud-sandbox" or connection.target != self.remote_id or connection.credential_ref:
            raise AdapterError("invalid sandbox connection")
        op, args = command.name, command.arguments
        if op == "submit":
            return self._submit(args[0], payload)
        if op == "probe":
            return {"ok": True, "capabilities_checked": {}}
        if op == "lookup":
            with self._lock:
                job_id = self._keys.get(args[0])
                return self._snapshot(job_id) if job_id else {"state": "not_found"}
        if not args:
            raise AdapterError("missing job id")
        job_id = args[0]
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise AdapterError("unknown job")
            if op == "cancel":
                if job["state"] == "running" and not job.get("local_stopped"):
                    with self._box["lock"]:
                        self._box["uncertain"] = True
                        try:
                            if self.state_store is not None:
                                self.state_store.mark_remote_unknown(self.remote_id)
                        finally:
                            job["cancel"].set()
                return self._snapshot(job_id)
            if op == "status":
                return self._snapshot(job_id)
            if op == "logs":
                return {"logs": job["logs"][int(args[1]):]}
            if op == "fetch":
                artifacts = job["artifacts"]
                if len(args) > 1:
                    artifacts = [a for a in artifacts if a["name"] == args[1]]
                return {"artifacts": copy.deepcopy(artifacts)}
        raise AdapterError("unsupported sandbox operation")

    def _snapshot(self, job_id):
        job = self._jobs[job_id]
        snapshot = {"job_id": job_id, "state": job["state"]}
        if job["state"] == "running" and job["cancel"].is_set():
            snapshot["cancel_requested"] = True
        if job.get("local_stopped") and job["cancel"].is_set():
            snapshot.update(local_state="stopped", remote_state="unknown")
        if "reason" in job:
            snapshot["reason"] = job["reason"]
        return snapshot

    def _submit(self, job_id, payload):
        if not isinstance(payload, Mapping) or payload.get("sensitivity") != "public" or payload.get("secrets"):
            raise AdapterError("sandbox requires public input without secrets")
        request = copy.deepcopy(dict(payload))
        # An explicit public classification is required; no local paths or file contents
        # are read implicitly. The shared policy must still authorize the request.
        _scan(request)
        if self.scan_outbound is not None:
            result = self.scan_outbound(copy.deepcopy(request))
            if result is False or (isinstance(result, (list, tuple, set)) and result):
                raise AdapterError("outbound content rejected")
        key = request["idempotency_key"]
        with self._lock:
            if key in self._keys or job_id in self._jobs:
                raise AdapterError("duplicate sandbox job")
            with self._box["lock"]:
                if self._box["active"]:
                    raise AdapterError("sandbox is busy")
                if self._box["uncertain"] or (self.state_store is not None and self.state_store.remote_unknown(self.remote_id)):
                    try:
                        self.driver.status()
                    except Exception:
                        pass
                    # ibox status reports machine state, not remaining processes.
                    raise AdapterError("sandbox residual processes cannot be verified")
                self._box["active"] = True
            job = {"state": "running", "cancel": threading.Event(), "logs": [], "artifacts": []}
            self._jobs[job_id] = job
            self._keys[key] = job_id
        try:
            if job["cancel"].is_set():
                raise InstaboxCancelled("ibox operation cancelled")
            response = self.driver.execute(request, cancel_event=job["cancel"])
            if not isinstance(response, Mapping):
                raise AdapterError("invalid sandbox response")
            logs = response.get("logs", [])
            names = response.get("artifacts", [])
            if not isinstance(logs, list) or not all(isinstance(x, str) for x in logs) or not isinstance(names, list) or not all(isinstance(x, str) for x in names):
                raise AdapterError("invalid sandbox response")
            if self.scan_inbound is not None and not self.scan_inbound("".join(logs).encode("utf-8")):
                raise AdapterError("sandbox output rejected")
            artifacts = []
            for name in names:
                if job["cancel"].is_set():
                    break
                content = self.driver.fetch(name, cancel_event=job["cancel"])
                if not isinstance(content, bytes):
                    raise AdapterError("invalid sandbox artifact")
                if self.scan_inbound is not None and not self.scan_inbound(content):
                    raise AdapterError("sandbox output rejected")
                digest = hashlib.sha256(content).hexdigest()
                hashes = response.get("artifact_hashes")
                if hashes is not None and (not isinstance(hashes, Mapping) or hashes.get(name) != digest):
                    raise AdapterError("sandbox artifact digest mismatch")
                artifacts.append({"name": name, "content": content, "size": len(content), "sha256": digest})
            with self._lock:
                job["logs"], job["artifacts"] = logs, artifacts
                if not job["cancel"].is_set():
                    job["state"] = "succeeded" if response.get("ok") is True else "failed"
        except Exception as exc:
            timeout_logs = []
            if isinstance(exc, InstaboxTimeout):
                try:
                    logs = exc.logs
                    if (all(isinstance(x, str) for x in logs)
                            and (self.scan_inbound is None
                                 or self.scan_inbound("".join(logs).encode("utf-8")))):
                        timeout_logs = list(logs)
                except Exception:
                    pass  # Reject output if the inbound scanner fails.
            with self._lock:
                if not job["cancel"].is_set():
                    job["state"] = "failed"
                if isinstance(exc, InstaboxTimeout) and not job["cancel"].is_set():
                    job["reason"] = "timeout"
                    job["logs"] = timeout_logs
                    with self._box["lock"]:
                        self._box["uncertain"] = True
                        if self.state_store is not None:
                            self.state_store.mark_remote_unknown(self.remote_id)
        finally:
            with self._lock:
                if job["cancel"].is_set():
                    job["local_stopped"] = True
                with self._box["lock"]:
                    self._box["active"] = False
        with self._lock:
            return self._snapshot(job_id)


class CloudSandboxAdapter(BaseEnvironmentAdapter):
    adapter_kind = "cloud-sandbox"

    def __init__(self, *, driver, remote_id: str, scan_outbound=None, scan_inbound=None, state_store=None):
        super().__init__(executor=CloudSandboxExecutor(driver, remote_id=remote_id, scan_outbound=scan_outbound, scan_inbound=scan_inbound, state_store=state_store), remote_id=remote_id)
