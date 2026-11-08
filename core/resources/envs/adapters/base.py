"""Shared implementation for local, ssh-host, and container adapters."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol

from host.lib.envs import Command, Connection, ConnectionLost, Executor

_JOB_ID = re.compile(r"^[a-z0-9-]{1,80}$")
_REFERENCE = re.compile(r"^[a-z][a-z0-9-]{0,127}$")
_TERMINAL = frozenset({"succeeded", "failed", "timed_out", "stalled", "cancelled", "lost", "rejected"})


class AdapterError(RuntimeError):
    """A contract error that applies only to the affected environment job."""


class CredentialReferenceError(AdapterError):
    """A job asked for a credential that the selected environment lacks."""


class ArtifactIntegrityError(AdapterError):
    """An environment returned an artifact whose declared metadata is false."""


class SecretDetected(AdapterError):
    """A recovered log or artifact matched the caller's secret detector."""


class ResidentWorkloadError(AdapterError):
    """A rental job left tracked work behind or could not prove tracking."""


class SubmissionUncertain(AdapterError):
    """A submit reply was lost; callers must use lookup before any retry."""


@dataclass(frozen=True)
class Artifact:
    name: str
    size: int
    sha256: str
    content: bytes


@dataclass(frozen=True)
class CollectedJob:
    logs: tuple[str, ...]
    artifacts: tuple[Artifact, ...]


class EnvironmentAdapter(Protocol):
    def submit(self, spec: Mapping[str, Any], remote_id: str | None = None) -> Mapping[str, Any]: ...

    def status(self, job_id: str) -> Mapping[str, Any]: ...

    def logs(self, job_id: str, since: int = 0) -> tuple[str, ...]: ...

    def fetch(self, job_id: str, name: str | None = None) -> tuple[Artifact, ...]: ...

    def cancel(self, job_id: str) -> Mapping[str, Any]: ...

    def probe(self) -> Mapping[str, Any]: ...

    def lookup(self, idempotency_key: str) -> Mapping[str, Any]: ...


class BaseEnvironmentAdapter:
    """A narrow wrapper over the external ``ax-env-run`` contract.

    It deliberately has no generic command, shell, deletion, or credential-value
    interface. Each public method maps to one of S-25.3's fixed operations.
    """

    adapter_kind = "base"

    def __init__(
        self,
        *,
        executor: Executor,
        remote_id: str,
        connection_credential: str | None = None,
        work_credentials: Iterable[str] = (),
    ) -> None:
        self._executor = executor
        self.remote_id = self._required_ref(remote_id, "remote_id")
        self.connection = Connection(
            kind=self.adapter_kind,
            target=self.remote_id,
            credential_ref=self._optional_ref(connection_credential, "connection_credential"),
        )
        self._work_credentials = frozenset(self._required_ref(name, "work_credential") for name in work_credentials)

    @staticmethod
    def _required_ref(value: object, field: str) -> str:
        if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
            raise CredentialReferenceError(f"invalid {field} reference")
        return value

    @classmethod
    def _optional_ref(cls, value: object, field: str) -> str | None:
        return None if value is None else cls._required_ref(value, field)

    @staticmethod
    def _job_id(value: object) -> str:
        if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
            raise AdapterError("invalid job_id")
        return value

    def _call(
        self, operation: str, *arguments: str, payload: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        return self._executor.execute(
            connection=self.connection,
            command=Command(operation, tuple(arguments)),
            payload=payload,
        )

    def submit(self, spec: Mapping[str, Any], remote_id: str | None = None) -> Mapping[str, Any]:
        if not isinstance(spec, Mapping):
            raise AdapterError("job specification must be an object")
        if remote_id is not None and remote_id != self.remote_id:
            raise AdapterError("remote_id does not match this adapter")
        job_id = self._job_id(spec.get("job_id"))
        idempotency_key = spec.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise AdapterError("idempotency_key is required")
        references = spec.get("secrets", ())
        if not isinstance(references, (list, tuple)) or not all(isinstance(item, str) for item in references):
            raise CredentialReferenceError("secrets must contain credential references only")
        unknown = set(references).difference(self._work_credentials)
        if unknown:
            raise CredentialReferenceError("environment does not register a requested credential")
        payload = dict(spec)
        payload["secrets"] = list(references)
        try:
            return self._call("submit", job_id, payload=payload)
        except ConnectionLost as exc:
            raise SubmissionUncertain("submit reply lost; resolve with lookup before retry") from exc

    def status(self, job_id: str) -> Mapping[str, Any]:
        response = self._call("status", self._job_id(job_id))
        state = response.get("state")
        if state not in {"queued", "running", "unknown", *_TERMINAL}:
            raise AdapterError("invalid state returned by environment")
        return response

    def logs(self, job_id: str, since: int = 0) -> tuple[str, ...]:
        if not isinstance(since, int) or since < 0:
            raise AdapterError("since must be a non-negative sequence")
        response = self._call("logs", self._job_id(job_id), str(since))
        logs = response.get("logs", ())
        if not isinstance(logs, (list, tuple)) or not all(isinstance(entry, str) for entry in logs):
            raise AdapterError("invalid logs returned by environment")
        return tuple(logs)

    def fetch(self, job_id: str, name: str | None = None) -> tuple[Artifact, ...]:
        args = (self._job_id(job_id),) if name is None else (self._job_id(job_id), str(name))
        response = self._call("fetch", *args)
        raw_artifacts = response.get("artifacts", ())
        if not isinstance(raw_artifacts, (list, tuple)):
            raise ArtifactIntegrityError("invalid artifact list")
        return tuple(self._artifact(item) for item in raw_artifacts)

    @staticmethod
    def _artifact(raw: object) -> Artifact:
        if not isinstance(raw, Mapping):
            raise ArtifactIntegrityError("invalid artifact")
        name, size, digest, content = raw.get("name"), raw.get("size"), raw.get("sha256"), raw.get("content")
        if not isinstance(name, str) or not name or not isinstance(size, int) or size < 0:
            raise ArtifactIntegrityError("artifact metadata is invalid")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ArtifactIntegrityError("artifact hash is invalid")
        if not isinstance(content, bytes):
            raise ArtifactIntegrityError("artifact content is unavailable")
        actual = hashlib.sha256(content).hexdigest()
        if len(content) != size or actual != digest:
            raise ArtifactIntegrityError("artifact size or content hash differs from its metadata")
        return Artifact(name=name, size=size, sha256=digest, content=content)

    def cancel(self, job_id: str) -> Mapping[str, Any]:
        return self._call("cancel", self._job_id(job_id))

    def probe(self) -> Mapping[str, Any]:
        return self._call("probe")

    def lookup(self, idempotency_key: str) -> Mapping[str, Any]:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise AdapterError("idempotency_key is required")
        return self._call("lookup", idempotency_key)

    def collect(self, job_id: str, secret_detector: Callable[[str], bool]) -> CollectedJob:
        logs = self.logs(job_id)
        artifacts = self.fetch(job_id)
        text = "\n".join(logs)
        if secret_detector(text) or any(secret_detector(item.content.decode("utf-8", "replace")) for item in artifacts):
            raise SecretDetected("recovered output contains a secret")
        return CollectedJob(logs=logs, artifacts=artifacts)

    def release_rental(self, job_id: str) -> Mapping[str, Any]:
        """Require zero tracked residents, then ask the remote runner to archive work."""
        checked_id = self._job_id(job_id)
        response = self.status(checked_id)
        if response.get("state") not in _TERMINAL:
            raise ResidentWorkloadError("rental job has not finished")
        cleanup = response.get("cleanup")
        if not isinstance(cleanup, Mapping):
            raise ResidentWorkloadError("rental cleanup was not reported")
        if cleanup.get("tracking") != "complete":
            raise ResidentWorkloadError("rental workload tracking is incomplete")
        if cleanup.get("resident_processes") != 0 or cleanup.get("resident_containers") != 0:
            raise ResidentWorkloadError("rental job left tracked resident work")
        released = self._call("release", checked_id)
        archived_to = released.get("archived_to")
        if not isinstance(archived_to, str) or not archived_to.startswith(f"archive/{checked_id}"):
            raise ResidentWorkloadError("rental work was not archived")
        return released
