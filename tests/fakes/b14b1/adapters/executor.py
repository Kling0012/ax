"""Stateful, in-memory ``ax-env-run`` fake used by adapter contract tests."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from host.lib.envs import Command, Connection, ConnectionLost


@dataclass(frozen=True)
class Request:
    connection: Connection
    command: Command
    payload: Mapping[str, Any] | None


class FakeEnvironmentExecutor:
    """A fake runner that persists job records across a simulated disconnect."""

    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.by_idempotency: dict[str, str] = {}
        self.disconnect_after: set[str] = set()
        self.probe_response: dict[str, Any] = {"reachable": True, "contract": "1.0"}

    def execute(
        self, *, connection: Connection, command: Command, payload: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        self.requests.append(Request(connection, command, payload))
        operation, args = command.name, command.arguments
        if operation == "submit":
            assert payload is not None
            job_id, key = args[0], payload["idempotency_key"]
            existing = self.by_idempotency.get(key)
            if existing is None:
                self.by_idempotency[key] = job_id
                self.jobs[job_id] = {
                    "state": "queued",
                    "logs": [],
                    "artifacts": [],
                    "cleanup": {"tracking": "complete", "resident_processes": 0, "resident_containers": 0},
                }
            else:
                job_id = existing
            response: Mapping[str, Any] = {"job_id": job_id, "state": self.jobs[job_id]["state"]}
        elif operation == "status":
            record = self.jobs[args[0]]
            response = {"job_id": args[0], "state": record["state"], "cleanup": dict(record["cleanup"])}
        elif operation == "logs":
            response = {"logs": list(self.jobs[args[0]]["logs"])}
        elif operation == "fetch":
            artifacts = list(self.jobs[args[0]]["artifacts"])
            response = {"artifacts": artifacts if len(args) == 1 else [item for item in artifacts if item["name"] == args[1]]}
        elif operation == "cancel":
            self.jobs[args[0]]["state"] = "cancelled"
            response = {"job_id": args[0], "state": "cancelled"}
        elif operation == "lookup":
            job_id = self.by_idempotency.get(args[0])
            response = {"job_id": job_id, "state": self.jobs[job_id]["state"] if job_id else "none"}
        elif operation == "probe":
            response = dict(self.probe_response)
        elif operation == "release":
            response = {"job_id": args[0], "archived_to": f"archive/{args[0]}"}
        else:
            raise AssertionError(f"unexpected fixed adapter operation: {operation}")
        if operation in self.disconnect_after:
            raise ConnectionLost(operation)
        return response

    def finish(
        self,
        job_id: str,
        *,
        logs: list[str] | None = None,
        artifacts: dict[str, bytes] | None = None,
        cleanup: Mapping[str, Any] | None = None,
    ) -> None:
        record = self.jobs[job_id]
        record["state"] = "succeeded"
        record["logs"] = list(logs or [])
        record["cleanup"] = dict(cleanup or record["cleanup"])
        record["artifacts"] = [
            {"name": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "content": body}
            for name, body in (artifacts or {}).items()
        ]
