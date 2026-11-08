"""Deterministic integration ports; no network or process execution."""
from copy import deepcopy
from types import SimpleNamespace

from plugins.subloop.change.integration import Ports

HEAD = "a" * 40
BASE = "b" * 40
NEXT = "c" * 40
MERGED = "d" * 40


def evidence(head=HEAD, *, base=BASE, attempt="head-1", role="head", execution=None, kind="all"):
    return {"repo": "owner/code", "base_sha": base, "head_sha": head, "recipe_version": "1",
            "command": "verify", "validation_kind": kind, "attempt": attempt,
            "started_at": "2026-11-07T00:00:00Z", "ended_at": "2026-11-07T00:00:01Z",
            "exit_code": 0, "timed_out": False, "complete": True, "output_tail": "passed",
            "test_names": ["acceptance"], "environment": "fake", "executed": True,
            "execution_id": execution or f"{head}-{attempt}-{kind}", "output_truncated": False,
            "failure_evidence": {}, "revision_role": role}


def target():
    return {"issue_repo": "owner/issues", "implementation_repo": "owner/code", "merge_method": "merge",
            "trusted_authors": ["trusted"], "ci_sources": ["ci/public"], "test_recipe": "fake",
            "allowed_paths": ["src/**"], "untrusted_forbidden_paths": [".github/**"],
            "outbound_allowlist": [], "execution_cap": 1, "priority_labels": []}


def item(number=1, *, trusted=True, route="pr"):
    return {"provider": "github", "issue_repo": "owner/issues", "kind": "pr", "number": number,
            "implementation_repo": "owner/code", "pr": number, "project": "example",
            "author": "trusted" if trusted else "external", "route": route,
            "title": "raw external title", "body": "raw external instruction"}


def snapshot(it=None):
    it = it or item()
    return {"repo": "owner/code", "pr": it["pr"], "head_sha": HEAD, "base_sha": BASE,
            "comments_complete": True, "unprocessed_comments": [], "input_revision": "input-1",
            "requirements_revision": "input-1", "required_ids": ["r1"],
            "requirements": [{"id": "r1", "source": "issue:1", "text": "acceptance", "status": "done", "evidence": ["test:acceptance"]}],
            "mergeable": True, "state": "open", "draft": False, "can_merge": True,
            "publication_head": HEAD, "outbound_checked": True, "ci_complete": True,
            "ci": [{"source": "ci/public", "head_sha": HEAD, "status": "success", "origin": "configured"}],
            "collect_only_reviewed_head": HEAD}


class Clock:
    def __init__(self):
        self.value = 1000.0
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


class Collection:
    def __init__(self):
        self.items, self.checkpoints, self.calls = [], [], []
    def collect(self, *, dry_run, mode):
        self.calls.append((dry_run, mode))
        return deepcopy(self.items)
    def checkpoint(self, item, state, reason):
        self.checkpoints.append((item["number"], state, reason))


class GitHub:
    def __init__(self):
        self.live = snapshot()
        self.merges, self.reads = [], 0
        self.on_read = None
        self.on_merge = None
        self.observed = {}
    def view(self, item, target):
        self.reads += 1
        if self.on_read:
            self.on_read(self, self.reads)
        return {**deepcopy(self.live), "pr": item["pr"]}
    def merge(self, repo, pr, *, sha, method):
        if self.on_merge:
            return self.on_merge(self, repo, pr, sha, method)
        self.merges.append((repo, pr, sha, method))
        if self.live["head_sha"] != sha:
            return {"merged": False}
        result = {"merged": True, "head_sha": sha, "merge_commit_sha": MERGED}
        self.observed[pr] = result
        return result
    def reconcile_merge(self, repo, pr, *, sha):
        return deepcopy(self.observed.get(pr, {}))
    def change_head(self):
        self.live.update(head_sha=NEXT, publication_head=NEXT, collect_only_reviewed_head=NEXT)
        self.live["ci"][0]["head_sha"] = NEXT
        self.live["mergeable"] = True


class Safety:
    def __init__(self):
        self.calls, self.fail = [], False
    def review(self, item):
        self.calls.append(deepcopy(item))
        live = item["snapshot"]
        return {"status": "failed" if self.fail else "passed", "head_sha": live["head_sha"],
                "input_revision": live["input_revision"], "worker_input": {"summaries": ["Implement acceptance", "Confirm acceptance"]},
                "reviews": [{"reviewer": f"reviewer-{i}", "verdict": "safe", "concerns": [], "summary": "neutral"} for i in (1, 2)]}


class Verification:
    def __init__(self):
        self.calls, self.fail_heads = [], set()
        self.parked = False
    def verify(self, item, target, *, head_sha, base_sha):
        self.calls.append(head_sha)
        records = [evidence(head_sha, base=base_sha)]
        if item["author"] not in target["trusted_authors"]:
            records = [evidence(base_sha, base=base_sha, attempt="base", role="base"),
                       *records, evidence(head_sha, base=base_sha, attempt="head-2")]
        return {"status": "parked" if self.parked else "failed" if head_sha in self.fail_heads else "passed",
                "reason": "verify_unavailable" if self.parked else "verified", "evidence": records,
                "comparisons": [], "unperformed": []}


class Work:
    def __init__(self, github):
        self.github, self.calls = github, []
        self.cause_known = True
    def plan(self, item, target, *, safety):
        return {"base_ref": BASE, "branch": "ai/change-1", "verify": "verify", "answer": "", "resume_ref": ""}
    def run(self, item, target, *, operation):
        self.calls.append((operation, deepcopy(item)))
        if operation == "tdd-change":
            self.github.change_head()
        return {"status": "passed", "cause_known": self.cause_known, "answer": "Supported answer",
                "evidence": ["test:acceptance"], "publication": {"head_sha": self.github.live["head_sha"], "body": "Current change", "diff": "src/file"}}


class Permission:
    def __init__(self):
        self.allowed = True
        self.grant = None
        self.cards, self.calls = [], []
    def check(self, **scope):
        self.calls.append(scope)
        return {"allowed": self.allowed, "reason": "configured"}
    def instruction(self, item, *, repo, pr, head_sha):
        return deepcopy(self.grant)
    def wait(self, item, **scope):
        if scope not in self.cards:
            self.cards.append(scope)
        return "card-1"
    def authorize(self, item, head=HEAD):
        self.grant = {"repo": item["implementation_repo"], "pr": item["pr"], "head_sha": head, "operation": "merge",
                      "source": "user", "card_id": "card-1", "instruction_ref": "instruction-1", "active": True}


class Deliveries:
    def __init__(self, clock):
        self.clock, self.sent, self.observed = clock, [], {}
        self.result = None
        self.on_send = None
    def reconcile(self, key, destination):
        return deepcopy(self.observed.get(key, {"status": "absent"}))
    def send(self, key, destination, body):
        self.sent.append((key, destination, deepcopy(body)))
        if self.on_send:
            return self.on_send(key, destination, body)
        result = self.result or {"status": "delivered", "delivered_at": self.clock(), "external_id": key}
        if result["status"] == "delivered":
            self.observed[key] = deepcopy(result)
        return deepcopy(result)


def make_ports(clock):
    gh, collection, safety, verify, perm = GitHub(), Collection(), Safety(), Verification(), Permission()
    work = Work(gh)
    notices, pubs, release = (Deliveries(clock) for _ in range(3))
    ports = Ports(collection, safety, verify, work, gh, perm, notices, pubs, release,
                  outbound_check=lambda body: True, scrub=lambda value: deepcopy(value))
    return SimpleNamespace(ports=ports, github=gh, collection=collection, safety=safety,
                           verification=verify, work=work, permissions=perm, notices=notices,
                           publications=pubs, release=release)
