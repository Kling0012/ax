"""Trusted adapters for change integration. Raw issue/PR fields never supply ports.

All calls are synchronous and bounded by their adapter. The b7 bridge runs them in
its worker thread. An uncertain write is reconciled, never retried blindly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class DependencyUnavailable(RuntimeError):
    pass


class GitHubPort(Protocol):
    def view(self, item: dict, target: dict) -> dict:
        """Fresh, fully paginated PR, comments, requirements, CI and mergeability.

        Shape is documented in docs/b14/integration/ports.md. Missing fields deny
        merging. No values in this snapshot count as user authorization.
        """
        ...

    def merge(self, repo: str, pr: int, *, sha: str, method: str) -> dict: ...
    def reconcile_merge(self, repo: str, pr: int, *, sha: str) -> dict: ...


class CollectionPort(Protocol):
    def collect(self, *, dry_run: bool, mode: str) -> list[dict]: ...
    def checkpoint(self, item: dict, state: str, reason: str) -> None: ...


class SafetyPort(Protocol):
    def review(self, item: dict) -> dict:
        """Returns status, bound head_sha/input_revision, two independent reviews."""
        ...


class VerificationPort(Protocol):
    def verify(self, item: dict, target: dict, *, head_sha: str, base_sha: str) -> dict: ...


class WorkPort(Protocol):
    def plan(self, item: dict, target: dict, *, safety: dict) -> dict:
        """Trusted TDD parameters; side-effect-free and revision-bound."""
        ...

    def run(self, item: dict, target: dict, *, operation: str) -> dict:
        """Prepare answer/publication, investigate, or run tdd-change within model_budget."""
        ...


class PermissionPort(Protocol):
    def check(self, *, project: str, repo: str, destination: str, operation: str) -> dict: ...

    def instruction(self, item: dict, *, repo: str, pr: int, head_sha: str) -> dict | None:
        """Only an authenticated, scoped ledger card answer can grant this."""
        ...

    def wait(self, item: dict, *, repo: str, pr: int | None, head_sha: str, reason: str) -> str: ...


class DeliveryPort(Protocol):
    def reconcile(self, delivery_id: str, destination: str) -> dict:
        """status: delivered|absent|retry|unknown. delivered needs delivered_at."""
        ...

    def send(self, delivery_id: str, destination: str, body: dict) -> dict: ...


@dataclass
class Ports:
    collection: CollectionPort
    safety: SafetyPort
    verification: VerificationPort
    work: WorkPort
    github: GitHubPort
    permissions: PermissionPort
    notifications: DeliveryPort
    publications: DeliveryPort
    release: DeliveryPort
    outbound_check: Callable[[dict], bool]
    scrub: Callable[[Any], Any]


def load_factory(spec: str, *, services, journal) -> Ports:
    """A deployment-owned module:factory, never a value from an item."""
    if not spec or ":" not in spec:
        raise DependencyUnavailable("change_ports_unconfigured")
    import importlib

    module, factory = spec.split(":", 1)
    result = getattr(importlib.import_module(module), factory)(services=services, journal=journal)
    if not isinstance(result, Ports):
        raise DependencyUnavailable("invalid_change_ports")
    return result
