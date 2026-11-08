"""S-19.1/19.2 collection boundary. No GitHub or worker calls at import time."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Iterable, Protocol

from axcommon.timeutil import now

EXECUTION_STATES = frozenset({"planned", "launching", "submitted", "running", "completed", "failed", "timed_out", "cancelled", "lost", "skipped", "ejected", "missed"})
MODES = frozenset({"run", "collect_only", "poll_paused", "draining"})
BUDGET_KNOBS = {name: {"who": ["inspection", "user"]} for name in (
    "change_starts_per_cycle", "change_model_calls_per_cycle", "change_cycle_seconds")}


def epoch() -> float:
    return now().timestamp()


@dataclass(frozen=True, order=True)
class ItemKey:
    provider: str
    issue_repo: str
    kind: str
    number: int

    def __post_init__(self):
        if self.provider != "github" or self.kind not in {"issue", "pr"}:
            raise ValueError("unsupported provider or kind")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.issue_repo):
            raise ValueError("owner/repo required")
        if type(self.number) is not int or self.number <= 0:
            raise ValueError("positive item number required")
        # GitHub repo names are case insensitive; this is also the queue tie-break key.
        object.__setattr__(self, "issue_repo", self.issue_repo.lower())

    def as_dict(self) -> dict:
        return {"provider": self.provider, "issue_repo": self.issue_repo, "kind": self.kind, "number": self.number}

    def encode(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def decode(cls, value: str) -> ItemKey:
        return cls(**json.loads(value))


@dataclass(frozen=True)
class Target:
    issue_repo: str
    implementation_repo: str
    merge_method: str
    ci_sources: list
    test_recipe: dict | str
    priority_labels: list[str]
    execution_cap: int
    allowed_paths: list[str]
    untrusted_forbidden_paths: list[str]
    trusted_authors: list[str]
    outbound_allowlist: list
    state_labels: dict[str, str] = field(default_factory=dict)
    contribution: dict = field(default_factory=dict)
    verify_runner: str | None = None
    base_ref: str | None = None
    project: str | None = None
    launcher_target: str | None = None
    launcher_recipe_id: str | None = None
    tdd_verify: str | None = None

    def __post_init__(self):
        for name in ("issue_repo", "implementation_repo"):
            normalized = ItemKey("github", getattr(self, name), "issue", 1).issue_repo
            object.__setattr__(self, name, normalized)
        if self.merge_method not in {"merge", "squash", "rebase"}:
            raise ValueError("invalid merge_method")
        if type(self.execution_cap) is not int or self.execution_cap < 0:
            raise ValueError("invalid execution_cap")
        for name in ("ci_sources", "priority_labels", "allowed_paths", "untrusted_forbidden_paths", "trusted_authors", "outbound_allowlist"):
            if not isinstance(getattr(self, name), list):
                raise ValueError(f"{name} must be a list")
        if not isinstance(self.test_recipe, (str, dict)) or not self.test_recipe or not self.allowed_paths or not self.untrusted_forbidden_paths:
            raise ValueError("test recipe and path restrictions required")
        for name in ("priority_labels", "allowed_paths", "untrusted_forbidden_paths", "trusted_authors"):
            if not all(isinstance(value, str) and value for value in getattr(self, name)):
                raise ValueError(f"invalid {name}")
        if not isinstance(self.state_labels, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in self.state_labels.items()):
            raise ValueError("invalid state_labels")
        if not any(p.startswith(".github/") for p in self.untrusted_forbidden_paths) or not any(p.startswith(("package.json", "pyproject.toml", "uv.lock", "pnpm-lock.yaml", "*.csproj")) for p in self.untrusted_forbidden_paths) or not any(p.startswith(("Dockerfile", "compose", "docker-compose", "scripts/")) for p in self.untrusted_forbidden_paths):
            raise ValueError("workflow, dependency and environment restrictions required")
        if self.project is not None and (not isinstance(self.project, str) or
                not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', self.project)):
            raise ValueError("invalid project")
        for name in ("launcher_target", "launcher_recipe_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[a-z0-9-]{1,40}', value)):
                raise ValueError(f"invalid {name}")
        if self.base_ref is not None:
            ref = self.base_ref
            if (not isinstance(ref, str) or not ref or ref.startswith(('-', '/', '.')) or
                    ref.endswith(('/', '.', '.lock')) or any(x in ref for x in ('..', '//', '@{')) or
                    not re.fullmatch(r'[A-Za-z0-9_./-]+', ref) or
                    any(part.startswith('.') or part.endswith('.lock') for part in ref.split('/'))):
                raise ValueError("invalid base_ref")
        if self.tdd_verify is not None and (not isinstance(self.tdd_verify, str) or
                not self.tdd_verify.strip() or '\x00' in self.tdd_verify or len(self.tdd_verify) > 8192):
            raise ValueError("invalid tdd_verify")
        if len(set(self.priority_labels)) != len(self.priority_labels):
            raise ValueError("duplicate priority label")


class TargetRegistry:
    """Invalid/ambiguous entries stop only their own issue repo."""

    def __init__(self, entries: Iterable[dict | Target]):
        self.targets: dict[str, Target] = {}
        self.errors: dict[str, str] = {}
        for i, entry in enumerate(entries):
            repo = str(entry.get("issue_repo", f"invalid:{i}") if isinstance(entry, dict) else entry.issue_repo).lower()
            try:
                target = entry if isinstance(entry, Target) else Target(**entry)
                if repo in self.targets or repo in self.errors:
                    raise ValueError("ambiguous_mapping")
                self.targets[repo] = target
            except (ValueError, TypeError):
                self.targets.pop(repo, None)
                self.errors[repo] = "invalid_or_ambiguous_target"

    @classmethod
    def load(cls, path):
        import yaml
        with open(path, encoding="utf-8") as source:
            data = yaml.safe_load(source)
        if not isinstance(data, dict) or not isinstance(data.get("targets"), list):
            raise ValueError("targets list required")
        return cls(data["targets"])

    def resolve(self, key: ItemKey) -> Target | None:
        return self.targets.get(key.issue_repo)


@dataclass(frozen=True)
class Snapshot:
    key: ItemKey
    version: str
    labels: tuple[str, ...] = ()
    external_state: str = "open"
    body: str = ""
    implementation_pr: dict | None = None


@dataclass(frozen=True)
class Observation:
    state: str  # S-3.4; unknown/unavailable must be returned as lost, never as success.
    external_id: str | None = None

    def __post_init__(self):
        if self.state not in EXECUTION_STATES - {"planned", "launching"}:
            raise ValueError("invalid external state")


class GitHubClient(Protocol):
    """All methods are injected. Pagination must be exhausted by the adapter."""
    def list_items(self, issue_repo: str, *, state: str) -> Iterable[Snapshot]: ...
    def list_comments(self, key: ItemKey) -> Iterable[dict[str, Any]]: ...
    def get_comment(self, key: ItemKey, external_id: str) -> dict | None: ...
    def post_comment(self, key: ItemKey, body: str, marker: str) -> str: ...
    def edit_comment(self, key: ItemKey, external_id: str, body: str) -> None: ...
    def get_labels(self, key: ItemKey) -> set[str]: ...
    def change_labels(self, key: ItemKey, *, add: set[str], remove: set[str]) -> None: ...
    def observe_work(self, key: ItemKey, external_id: str) -> Observation: ...


def paths_overlap(left: Iterable[str], right: Iterable[str]) -> bool:
    """Conservative glob prefixes: an unknown/dynamic path conflicts with everything."""
    def prefix(path):
        if not path or path.startswith("/") or ".." in path.split("/"):
            return ""
        return re.split(r"[*?\[]", path, maxsplit=1)[0].rstrip("/")
    for a in left:
        for b in right:
            a, b = prefix(a), prefix(b)
            if not a or not b or a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
            # A wildcard may match any suffix in its final path segment.
            if (not a.endswith("/") and b.startswith(a)) or (not b.endswith("/") and a.startswith(b)):
                return True
    return False
