"""B8 の偽の時系列記録と偽の台帳（FakeTransport に載せる）。ソケットを使わない。

FakeTimeline:
  POST /v1/events        1 件か配列。同じ id は無視して成功（S-3.5）。seq を付ける。health.beat は health 投影に反映する
  GET  /v1/events        after、kind、limit で絞る。応答は {"events": […]}（形は S-3.5 に無い。sources.extract_events は配列も許す）
  GET  /v1/projections/X health は beat から作る。plugins、controls、jobs は set_rows で入れる
  stop()/start()         止める間は接続できない（Unavailable）
FakeLedger: POST /v1/todos を記録する。
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

from axcommon import ids
from axcommon.client import Unavailable

TIMELINE = "http://timeline:8080"
LEDGER = "http://ledger:8080"


class FakeTimeline:
    def __init__(self, transport, clock=None):
        self.events: list[dict] = []
        self.seen: set[str] = set()
        self.posts = 0
        self.rows: dict[str, dict[str, dict]] = {"plugins": {}, "controls": {}, "jobs": {}}
        self.health: dict[str, dict] = {}
        self._down = False
        self.clock = clock
        transport.add("POST", "/v1/events", handler=self._post)
        transport.add("GET", "/v1/events", handler=self._get)
        for name in ("health", "plugins", "controls", "jobs"):
            transport.add("GET", f"/v1/projections/{name}", handler=lambda call, n=name: self._proj(call, n))

    # --- 操作 ---
    def stop(self):
        self._down = True

    def start(self):
        self._down = False

    def set_rows(self, name: str, rows: dict[str, dict]):
        self.rows[name] = rows

    def beat(self, component: str, ts: str, lag_s: float = 0.0, host: str = "h"):
        self.health[component] = {"host": host, "last_beat_at": ts, "uptime_s": 1.0, "lag_s": lag_s}

    def add(self, kind: str, component: str, data: dict | None = None, *, ts: str | None = None, subject: str = "x",
            cause: str | None = None, event_id: str | None = None) -> dict:
        if ts is None:
            ts = self.clock.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        ev = {"id": event_id or ids.new_ulid(), "ts": ts,
              "source": {"component": component, "host": "h"}, "project": None, "subloop": None, "kind": kind, "subject": subject,
              "data": data or {}, "corr": None, "cause": cause}
        self._append(ev)
        return ev

    def of_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]

    # --- ハンドラ ---
    def _append(self, ev: dict):
        if ev["id"] in self.seen:
            return
        self.seen.add(ev["id"])
        ev = dict(ev, seq=len(self.events) + 1)
        self.events.append(ev)
        if ev["kind"] == "health.beat":
            d = ev["data"]
            self.health[ev["source"]["component"]] = {"host": ev["source"]["host"], "last_beat_at": ev["ts"],
                                                       "uptime_s": d.get("uptime_s", 0), "lag_s": d.get("lag_s", 0)}

    def _post(self, call):
        if self._down:
            raise Unavailable("ConnectError")
        body = call.json
        for ev in (body if isinstance(body, list) else [body]):
            self.posts += 1
            self._append(ev)
        return 200, {}, b'{"ok":true}'

    def _get(self, call):
        if self._down:
            raise Unavailable("ConnectError")
        q = {k: v[0] for k, v in parse_qs(call.query).items()}
        after = int(q.get("after", 0))
        limit = int(q.get("limit", 200))
        out = [e for e in self.events if e["seq"] > after and (not q.get("kind") or e["kind"] == q["kind"])]
        return 200, {}, json.dumps({"events": out[:limit]}).encode()

    def _proj(self, call, name: str):
        if self._down:
            raise Unavailable("ConnectError")
        src = self.health if name == "health" else self.rows[name]
        rows = [{"key": k, "value": v, "upto_seq": 1} for k, v in src.items()]
        return 200, {}, json.dumps({"name": name, "upto_seq": len(self.events), "rows": rows}).encode()


class FakeLedger:
    def __init__(self, transport):
        self.todos: list[dict] = []
        self._down = False
        transport.add("POST", "/v1/todos", handler=self._post)

    def stop(self):
        self._down = True

    def start(self):
        self._down = False

    def _post(self, call):
        if self._down:
            raise Unavailable("ConnectError")
        if any(t.get("id") == call.json.get("id") for t in self.todos if call.json.get("id")):
            return 409, {}, b'{"error":{"code":"already_exists","message":"fake"}}'
        self.todos.append(call.json)
        return 201, {}, json.dumps(call.json).encode()
