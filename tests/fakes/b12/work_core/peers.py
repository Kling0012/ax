"""Offline peers with the existing AX wire shapes, not real services."""
from __future__ import annotations

import copy
import json
from urllib.parse import urlsplit, parse_qs
from types import SimpleNamespace

from core.intake.dest.b12.common import Engine
from core.intake.dest.b12.common.store import Store


class Peers:
    def __init__(self):
        self.calls, self.todos, self.items, self.jobs = [], {}, {}, []
        self.full, self.lost, self.pages = False, False, {}
        self.histories, self.next_seq = {}, 0

    def __call__(self, method, url, headers, raw, timeout):
        path = urlsplit(url).path
        body = json.loads(raw) if raw else None
        self.calls.append((method, url, copy.deepcopy(body)))
        status, result = 200, {}
        if self.lost:
            from axcommon.client import Unavailable
            raise Unavailable("fake outage")
        if method == "POST" and path == "/v1/todos":
            if body["id"] in self.todos:
                status, result = 409, {"error": {"code": "already_exists"}}
            else:
                self.todos[body["id"]] = copy.deepcopy(body)
                self.next_seq += 1
                self.histories[body["id"]] = [{"seq": self.next_seq, "actor": "intake", "kind": "created", "after": copy.deepcopy(body)}]
                result = body
        elif method == "GET" and path.startswith("/v1/todos/"):
            key = path.rsplit("/", 1)[1]
            result = copy.deepcopy(self.todos[key])
            if parse_qs(urlsplit(url).query).get("history") == ["1"]:
                result["history"] = copy.deepcopy(self.histories[key])
        elif method == "POST" and path.endswith("/transition"):
            from axcommon import schemas
            schemas.validate("todo-transition", body)
            key = path.split("/")[-2]
            current = self.todos[key]["state"]
            allowed = {"todo": {"running", "parked"}, "running": {"done", "parked"}, "parked": {"todo"}}
            expected = body.get("expected_history_seq")
            if expected is not None and expected != self.histories[key][-1]["seq"]:
                status, result = 409, {"error": {"code": "stale_version"}}
            elif body["to"] not in allowed.get(current, set()):
                status, result = 409, {"error": {"code": "invalid_transition"}}
            else:
                self.todos[key].update(state=body["to"], reason=body.get("reason"), check_at=body.get("check_at"))
                self.next_seq += 1
                self.histories[key].append({"seq": self.next_seq, "actor": "intake", "kind": f"transition:{current}->{body['to']}", "after": copy.deepcopy(self.todos[key])})
        elif method == "POST" and path == "/v1/plugins/work-tdd/items":
            from axcommon import schemas
            schemas.validate("work-item", body)
            if self.full:
                status, result = 429, {"error": {"code": "inbox_full"}}
            else:
                duplicate = body["item_key"] in self.items
                self.items[body["item_key"]] = copy.deepcopy(body)
                result = {"accepted": True, "duplicate": duplicate, "item_key": body["item_key"]}
        elif method == "POST" and path == "/v1/jobs":
            from axcommon import schemas
            schemas.validate("job-request", body)
            self.jobs.append(body)
            result = {"job_id": "job-1", "state": "queued"}
        elif method == "GET" and path == "/v1/events":
            params = parse_qs(urlsplit(url).query)
            kind = params["kind"][0]
            cursor = int(params.get("after", [0])[0])
            events = [ev for ev in self.pages.get(kind, []) if ev["seq"] > cursor]
            result = {"events": events, "next_after": max([cursor] + [ev["seq"] for ev in events])}
        else:
            raise AssertionError((method, url))
        return status, {}, json.dumps(result).encode()


class Backend:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.sha = "a" * 40

    def step(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError("fake " + name)

    def snapshot(self, branch):
        self.step("snapshot")
        return self.sha

    def verify(self, sha):
        assert sha == self.sha
        self.step("verify")
        return True

    def merge(self, sha):
        self.step("merge")
        return sha

    def tag(self, sha):
        self.step("tag")


def setup(path=None):
    peers, backend = Peers(), Backend()
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(transport=peers, dest_handlers={}), ext_failures=[]))
    deployments = []
    def deploy(key):
        deployments.append(key)
        return "fake-deploy.req"
    app.state.b12 = Engine(app, store=Store(path), backend=backend, deploy=deploy)
    return app, peers, backend, deployments


def item(kind="work", key="todo-1"):
    return {"item_key": key, "todo_id": key, "kind": kind, "project": "ax", "role": "impl", "title": "Example",
            "body": "Change the target only", "acceptance": {"examples": ["passes"], "boundaries": ["no deploy on failure"]},
            "branch": {"work": "ai/", "core_change": "core/", "subloop_create": "subloop/"}[kind] + key,
            **({"template": "templates/subloop"} if kind == "subloop_create" else {})}


def completion(kind="core_change", key="todo-1", seq=1):
    return {"seq": seq, "kind": "item.state", "source": {"component": "work-tdd"},
            "data": {"item_id": key, "todo_id": key, "state": "completed", "artifacts": {"branch": "core/forged"}}}


def approval(app, payload=None):
    from core.intake.approval.store import payload_digest
    payload = payload or {"spec": "an explicit change"}
    row = {"destination": "core.change", "state": "granted", "granted_at": "2026-11-07T00:00:00Z",
           "digest": payload_digest(payload), "payload_json": json.dumps(payload)}
    app.state.approvals = SimpleNamespace(store=SimpleNamespace(get=lambda key: row if key == "approval-1" else None))
    return row
