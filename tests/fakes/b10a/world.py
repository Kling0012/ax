"""B10a の試験の偽の世界。ホスト名とパスで振り分ける 1 つの転送（axcommon.client の Transport と同じ形）。ソケットを使わない。

  w = World()
  w.timeline   事象の保存と export。GET /v1/events?kind=…&after=…
  w.ledger     ToDo。POST /v1/todos（トークンが要る。409 は同じ id）、GET /v1/todos?state=、GET /v1/todos/{id}
  w.control    プラグインと命令。GET /v1/plugins、/v1/plugins/{id}、/effective、/v1/commands?state=&issued_by=、POST …/release
  w.pool       worker-pool のジョブ一覧 GET /v1/jobs
  w.ntfy       POST /（JSON）。受け付けたら {"id": …}。fail_next、down で失敗させる
  w.intake     POST /v1/instructions（ledger.complete と ledger.park を偽の台帳に適用する。apply=False で適用しない）
  w.emergency  POST /v1/emergency を EmergencyRunner につなぐ（試験で runner を渡す）
各部品は down() で接続できなくなる（Unavailable）。呼び出しの記録は w.calls（(host, method, path)）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from axcommon import ids, timeutil
from axcommon.client import Unavailable

TRANSITIONS = {
    "todo": {"running", "waiting_user", "parked", "dropped"},
    "running": {"review", "waiting_user", "parked", "todo", "done"},
    "review": {"done", "todo", "parked"},
    "waiting_user": {"todo", "done", "dropped"},
    "parked": {"todo", "dropped"},
    "done": set(),
    "dropped": set(),
}


def _resp(status: int, body: Any = None) -> tuple[int, dict, bytes]:
    return status, {"Content-Type": "application/json"}, (json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b"")


def err(status: int, code: str, message: str = "") -> tuple[int, dict, bytes]:
    return _resp(status, {"error": {"code": code, "message": message}})


class Part:
    def __init__(self):
        self._down = False
        self.hits = 0

    def down(self):
        self._down = True

    def up(self):
        self._down = False

    def check(self):
        self.hits += 1
        if self._down:
            raise Unavailable("ConnectError")


class FakeTimeline(Part):
    def __init__(self):
        super().__init__()
        self.events: list[dict] = []
        self.seen: set[str] = set()
        self.export_calls: list[dict] = []
        self.exports: list[list[dict]] = []

    def add(self, kind: str, data: dict | None = None, *, ts: str | None = None, component: str = "observer", subject: str = "x") -> dict:
        ev = {"id": ids.new_ulid(), "ts": ts or timeutil.now_ts(), "source": {"component": component, "host": "h"}, "project": None,
              "subloop": None, "kind": kind, "subject": subject, "data": data or {}, "corr": None, "cause": None}
        self._append(ev)
        return ev

    def _append(self, ev: dict) -> None:
        if ev["id"] in self.seen:
            return
        self.seen.add(ev["id"])
        self.events.append(dict(ev, seq=len(self.events) + 1))

    def of_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]

    def handle(self, method, path, q, body):
        self.check()
        if path == "/v1/events" and method == "POST":
            for ev in (body if isinstance(body, list) else [body]):
                self._append(ev)
            return _resp(200, {"ok": True})
        if path == "/v1/events" and method == "GET":
            after = int(q.get("after", 0))
            limit = int(q.get("limit", 200))
            out = [e for e in self.events if e["seq"] > after and (not q.get("kind") or e["kind"] == q["kind"])]
            return _resp(200, {"events": out[:limit]})
        if path == "/v1/export" and method == "POST":
            self.export_calls.append(body or {})
            self.exports.append(json.loads(json.dumps(self.events)))
            return _resp(200, {"exported": len(self.events), "files": ["timeline-export.jsonl"], "last_seq": len(self.events)})
        return err(404, "not_found")


class FakeLedger(Part):
    def __init__(self):
        super().__init__()
        self.todos: dict[str, dict] = {}
        self.require_token = True
        self.created_with_token: list[str | None] = []
        self.reject_create_status: int | None = None

    def put(self, **kw) -> dict:
        t = {"id": kw.pop("id", None) or ids.new_ulid(), "state": "todo", "owner": "system", "title": "t", "card": None,
             "updated_at": timeutil.now_ts()}
        t.update(kw)
        self.todos[t["id"]] = t
        return t

    def waiting(self, title="質問", question="どちらにしますか", *, version=1, reason="preference", **kw) -> dict:
        card = {"question": question, "options": ["A", "B"], "answer": None, "version": version, "waiting_reason": reason,
                "notification_state": "not_notified"}
        return self.put(state="waiting_user", title=title, card=card, owner="user", **kw)

    def answer(self, todo_id: str, text: str = "A") -> None:
        t = self.todos[todo_id]
        t["card"] = dict(t["card"], answer={"answer": text})
        t["state"] = "todo"

    def transition(self, todo_id: str, to: str) -> bool:
        t = self.todos.get(todo_id)
        if t is None or to not in TRANSITIONS.get(t["state"], set()):
            return False
        t["state"] = to
        t["updated_at"] = timeutil.now_ts()
        return True

    def handle(self, method, path, q, body, headers):
        self.check()
        if path == "/v1/todos" and method == "POST":
            if self.require_token and "Authorization" not in headers:
                return err(401, "unauthorized")
            if self.reject_create_status:
                return err(self.reject_create_status, "rejected")
            self.created_with_token.append(headers.get("Authorization"))
            if body.get("id") in self.todos:
                return err(409, "exists")
            t = self.put(**body)
            return _resp(201, t)
        if path == "/v1/todos" and method == "GET":
            rows = [t for t in self.todos.values() if not q.get("state") or t["state"] == q["state"]]
            return _resp(200, {"todos": rows})
        m = re.fullmatch(r"/v1/todos/([^/]+)", path)
        if m and method == "GET":
            t = self.todos.get(m.group(1))
            return _resp(200, t) if t else err(404, "not_found")
        if path == "/v1/promises" and method == "GET":
            return _resp(200, {"promises": getattr(self, "promises", [])})
        return err(404, "not_found")


class FakeControl(Part):
    def __init__(self):
        super().__init__()
        self.plugins: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.released: list[tuple[str, str | None]] = []   # (id, 使ったトークン)

    def add_plugin(self, pid: str, capabilities=("run", "draining", "stopped")) -> None:
        self.plugins[pid] = {"id": pid, "kind": "subloop", "manifest": {"capabilities": list(capabilities)}}

    def add_command(self, issued_by: str, target: str, op: str = "set_mode", args: dict | None = None, state: str = "active") -> str:
        cid = ids.new_ulid()
        self.commands[cid] = {"id": cid, "issued_by": issued_by, "target": target, "op": op, "args": args or {"mode": "draining"},
                              "state": state}
        return cid

    def mode_of(self, pid: str) -> str:
        default = "run" if "run" in self.plugins[pid]["manifest"]["capabilities"] else self.plugins[pid]["manifest"]["capabilities"][0]
        mode = default
        for c in self.commands.values():
            if c["state"] == "active" and c["op"] == "set_mode" and c["target"] in (pid, "all"):
                mode = c["args"]["mode"]
        return mode

    def handle(self, method, path, q, body, headers):
        self.check()
        if path == "/v1/plugins":
            return _resp(200, {"plugins": [{k: v for k, v in p.items() if k != "manifest"} for p in self.plugins.values()]})
        m = re.fullmatch(r"/v1/plugins/([^/]+)(/effective)?", path)
        if m:
            p = self.plugins.get(m.group(1))
            if p is None:
                return err(404, "not_found")
            if m.group(2):
                return _resp(200, {"plugin": p["id"], "mode": self.mode_of(p["id"]), "knobs": {}, "revision": 1})
            return _resp(200, {**p, "mode": self.mode_of(p["id"])})
        if path == "/v1/commands" and method == "GET":
            rows = [c for c in self.commands.values() if (not q.get("state") or c["state"] == q["state"])
                    and (not q.get("issued_by") or c["issued_by"] == q["issued_by"])]
            return _resp(200, {"commands": rows})
        m = re.fullmatch(r"/v1/commands/([^/]+)/release", path)
        if m and method == "POST":
            c = self.commands.get(m.group(1))
            if c is None:
                return err(404, "not_found")
            c["state"] = "released"
            self.released.append((c["id"], headers.get("Authorization")))
            return _resp(200, c)
        return err(404, "not_found")


class FakePool(Part):
    def __init__(self):
        super().__init__()
        self.jobs = [{"id": "job-1", "state": "running", "role": "impl"}]

    def handle(self, method, path, q, body):
        self.check()
        if path == "/v1/jobs" and method == "GET":
            return _resp(200, {"jobs": self.jobs})
        return err(404, "not_found")


class FakeNtfy(Part):
    def __init__(self):
        super().__init__()
        self.sent: list[dict] = []
        self._fail = 0
        self._status = 503
        self.no_id = False

    def fail_next(self, n: int = 1, status: int = 503):
        self._fail, self._status = n, status

    def handle(self, method, path, body):
        self.check()
        if method != "POST" or path != "/":
            return err(404, "not_found")
        if self._fail > 0:
            self._fail -= 1
            return err(self._status, "fake_failure")
        self.sent.append(body)
        return _resp(200, {} if self.no_id else {"id": f"ntfy-{len(self.sent)}", "event": "message", "topic": body.get("topic")})


class FakeIntake(Part):
    """POST /v1/instructions。ledger.complete / ledger.park を偽の台帳へ適用する（apply=False で適用せず成功だけ返す）。"""

    def __init__(self, ledger: FakeLedger):
        super().__init__()
        self.ledger = ledger
        self.instructions: list[dict] = []
        self.apply = True
        self.reject: tuple[int, str] | None = None
        self.seen_keys: dict[str, dict] = {}

    def handle(self, method, path, body):
        self.check()
        if path != "/v1/instructions" or method != "POST":
            return err(404, "not_found")
        key = body.get("idempotency_key")
        self.instructions.append(body)
        if key in self.seen_keys:
            return _resp(200, {**self.seen_keys[key], "replayed": True})
        if self.reject:
            return err(*self.reject)
        dest, payload = body["destination"], body["payload"]
        if self.apply and dest in ("ledger.complete", "ledger.park"):
            to = "done" if dest == "ledger.complete" else "parked"
            if not self.ledger.transition(payload["todo_id"], to):
                return err(409, "invalid_transition")
        out = {"ok": True, "instruction_id": body.get("id"), "destination": dest, "status": 200, "result": {}, "replayed": False}
        if key:
            self.seen_keys[key] = out
        return _resp(200, out)


class World:
    """ホスト名で振り分ける転送。"""

    def __init__(self):
        self.timeline = FakeTimeline()
        self.ledger = FakeLedger()
        self.control = FakeControl()
        self.pool = FakePool()
        self.ntfy = FakeNtfy()
        self.intake = FakeIntake(self.ledger)
        self.calls: list[tuple[str, str, str]] = []
        self.extra: dict[str, Callable] = {}   # host -> handler(method, path, q, body, headers)

    def paths(self, host: str, method: str | None = None) -> list[str]:
        return [p for h, m, p in self.calls if h == host and (method is None or m == method)]

    def __call__(self, method, url, headers, body, timeout):
        u = urlsplit(url)
        host = u.hostname or ""
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        data = json.loads(body.decode("utf-8")) if body else None
        self.calls.append((host, method, u.path))
        if host in self.extra:
            return self.extra[host](method, u.path, q, data, headers)
        if host == "timeline":
            return self.timeline.handle(method, u.path, q, data)
        if host == "ledger":
            return self.ledger.handle(method, u.path, q, data, headers)
        if host == "control":
            return self.control.handle(method, u.path, q, data, headers)
        if host == "worker-pool":
            return self.pool.handle(method, u.path, q, data)
        if host.startswith("ntfy"):
            return self.ntfy.handle(method, u.path, data)
        if host == "intake":
            return self.intake.handle(method, u.path, data)
        return err(404, "no_host")
