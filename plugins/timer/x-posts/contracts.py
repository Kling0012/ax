"""Internal Python ports; the B16b wire schemas remain the public contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class Target:
    plugin_id: str
    project: str
    repo: str
    destination: str
    account: str
    operations: tuple[str, ...] = ("投稿",)

    def delivery_target(self) -> dict:
        return {key: getattr(self, key) for key in
                ("plugin_id", "project", "repo", "destination")} | {
                    "operations": list(self.operations)}


@dataclass(frozen=True)
class Draft:
    draft_id: str
    text: str
    due_at: str
    images: tuple[str, ...] = ()
    artifacts: Mapping[str, bytes] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Inspection:
    """All four checks must finish; error and rejection are different outcomes."""
    public: str
    secret: str
    privacy: str
    yomiyasu: str

    @property
    def outcome(self) -> str:
        states = (self.public, self.secret, self.privacy, self.yomiyasu)
        if any(state not in ("passed", "rejected") for state in states):
            return "error"
        return "passed" if all(state == "passed" for state in states) else "rejected"


@dataclass(frozen=True)
class EnvResult:
    """Decoded env.run outcome. Exceptions without proof are ambiguous sends."""
    state: str
    env_id: str = ""
    output: Mapping = field(default_factory=dict, repr=False)
    definitely_not_sent: bool = False


class EnvironmentRunner(Protocol):
    def run(self, request: dict, artifacts: Mapping[str, bytes]) -> EnvResult:
        """Stage inputs and invoke the registered env.run entry through B14b2."""
        ...


class Control(Protocol):
    def targets(self) -> Sequence[dict]: ...
    def effective(self, plugin_id: str) -> dict: ...
    def check(self, target: dict, operation: str) -> bool: ...


class DraftWorker(Protocol):
    def generate(self, target: Target, period: str, statistics: Mapping,
                 materials: Sequence[Mapping]) -> Sequence[Draft]:
        """Use worker-pool; no X credentials or environment access are supplied."""
        ...


class Inspector(Protocol):
    def inspect(self, target: Target, draft: Draft, phase: str) -> Inspection: ...


class Events(Protocol):
    def emit(self, kind: str, subject: str, data: dict) -> object: ...


LostReporter = Callable[[dict], None]
