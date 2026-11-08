"""故障注入の偽の網（B13。tests/fakes/fault/）。部品を止める・遅らせる・偽の応答を返す・応答だけ失う・届かない経路を作る。

axcommon.client の Transport（呼び出し可能な物）を返す。ソケットを使わない。

使い方:
    net = FaultNet(clock=fake_clock)
    net.add("timeline", timeline_testclient, port=8080)          # ASGI（TestClient）か Transport 互換の呼び出し可能な物
    net.add("ledger", FakeTransport(), port=8080)
    t = net.transport(caller="manager", zone="core")             # ApiClient(url, transport=t) に渡す

    net.stop("timeline")                  # 以後は接続できない（Unavailable）。net.start("timeline") で戻る
    net.delay("ledger", 30, path="/v1/todos")     # 30 秒遅れる（clock があれば進め、要求の timeout を超えたら Unavailable）
    net.respond("ledger", 503, times=2)   # 偽の応答（本物には届けない）。times 回だけ。None なら外すまで
    net.lose_response("control", times=1) # 本物は処理するが、呼び出し側には届かない（時間切れ）
    net.stop("timeline", caller="pa")     # caller=… を付けると、その呼び出し元からだけ届かない
    net.clear("ledger")                   # 注入した故障をすべて外す
    net.calls                             # 記録（caller, zone, service, port, method, path, outcome）
    net.apply_f184()                      # F-18.4 の到達表（ax-work から ax-core へ届かない、作業側の口は 8081 だけ）を有効にする

ホスト名は `http://<service>:<port>` の <service>。サービスごとに登録した port 以外は届かない（refused）。
"""
from __future__ import annotations

import fnmatch
import json as _json
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from axcommon.client import Unavailable

# F-18.4 の網の所属。値は所属する網。口が 2 つのサービスは ax-core の 8080 と ax-work の 8081
CORE_ONLY = ("intake", "manager", "observer", "inspector", "hq", "archiver", "gate")
BOTH = ("timeline", "ledger", "control", "resources", "memory-connector", "worker-pool")
WORK_ONLY_PATTERNS = ("sl-*", "timer-runner")


def zones_of(name: str) -> tuple[str, ...]:
    if name in CORE_ONLY:
        return ("core",)
    if name in BOTH:
        return ("core", "work")
    if name == "egress-proxy":
        return ("work", "job")
    if any(fnmatch.fnmatch(name, p) for p in WORK_ONLY_PATTERNS):
        return ("work",)
    return ("core",)


@dataclass
class Fault:
    kind: str                       # down / delay / respond / lose
    service: str
    port: int | None = None
    method: str | None = None
    path: str | None = None         # fnmatch
    caller: str | None = None       # 呼び出し元（transport(caller=…) の名前）に限る。None は誰からでも
    seconds: float = 0.0
    status: int = 503
    body: Any = None
    times: int | None = None        # None は外すまで
    hits: int = 0

    def matches(self, service: str, port: int, method: str, path: str, caller: str | None = None) -> bool:
        return (self.service == service and (self.caller is None or self.caller == caller) and (self.port is None or self.port == port) and (self.method is None or self.method == method)
                and (self.path is None or fnmatch.fnmatch(path, self.path)) and (self.times is None or self.hits < self.times))


@dataclass
class Call:
    caller: str | None
    zone: str
    service: str
    port: int
    method: str
    path: str
    outcome: str                    # delivered / down / refused / faked / lost / delayed_timeout / no_route
    status: int | None = None


class _Backend:
    def __init__(self, target: Any, port: int):
        self.target, self.port = target, port

    def __call__(self, method, path_qs, headers, body, timeout, url):
        t = self.target
        if hasattr(t, "request"):
            # starlette の TestClient（httpx.Client）。Transport 互換の呼び出し可能な物は .request を持たない
            r = t.request(method, path_qs, headers={k: v for k, v in headers.items() if k.lower() != "host"}, content=body)
            return r.status_code, dict(r.headers), r.content
        return t(method, url, headers, body, timeout)


class FaultNet:
    def __init__(self, clock: Any = None):
        self.clock = clock
        self._services: dict[tuple[str, int], _Backend] = {}
        self._zones: dict[str, tuple[str, ...]] = {}
        self.faults: list[Fault] = []
        self.calls: list[Call] = []
        self._f184 = False
        self.default_timeout_s = 10.0

    # ---- 登録 ----
    def add(self, name: str, backend: Any, *, port: int = 8080, zones: tuple[str, ...] | None = None) -> "FaultNet":
        self._services[(name, port)] = _Backend(backend, port)
        if zones:
            self._zones[name] = zones
        return self

    def remove(self, name: str, port: int | None = None) -> None:
        for k in [k for k in self._services if k[0] == name and (port is None or k[1] == port)]:
            del self._services[k]

    def apply_f184(self, on: bool = True) -> "FaultNet":
        self._f184 = on
        return self

    # ---- 故障 ----
    def _add(self, f: Fault) -> Fault:
        self.faults.append(f)
        return f

    def stop(self, name: str, port: int | None = None, *, caller: str | None = None) -> Fault:
        return self._add(Fault("down", name, port, caller=caller))

    def start(self, name: str, port: int | None = None, *, caller: str | None = None) -> None:
        self.faults = [f for f in self.faults if not (f.kind == "down" and f.service == name and (port is None or f.port == port)
                                                      and (caller is None or f.caller == caller))]

    def delay(self, name: str, seconds: float, *, path: str | None = None, method: str | None = None, port: int | None = None,
              times: int | None = None, caller: str | None = None) -> Fault:
        return self._add(Fault("delay", name, port, method, path, caller=caller, seconds=seconds, times=times))

    def respond(self, name: str, status: int = 503, json: Any = None, *, path: str | None = None, method: str | None = None,
                port: int | None = None, times: int | None = None, caller: str | None = None) -> Fault:
        return self._add(Fault("respond", name, port, method, path, caller=caller, status=status, body=json, times=times))

    def flaky(self, name: str, n: int = 1, status: int = 503, **kw) -> Fault:
        return self.respond(name, status, times=n, **kw)

    def lose_response(self, name: str, *, times: int | None = 1, path: str | None = None, method: str | None = None,
                      port: int | None = None, caller: str | None = None) -> Fault:
        return self._add(Fault("lose", name, port, method, path, caller=caller, times=times))

    def clear(self, name: str | None = None) -> None:
        self.faults = [f for f in self.faults if name is not None and f.service != name]

    def is_stopped(self, name: str, caller: str | None = None) -> bool:
        return any(f.kind == "down" and f.service == name and f.caller in (None, caller) for f in self.faults)

    # ---- 呼び出し ----
    def transport(self, caller: str | None = None, zone: str = "core") -> Callable:
        def _t(method, url, headers, body, timeout):
            return self._deliver(caller, zone, method, url, headers, body, timeout)

        return _t

    def _reachable(self, zone: str, service: str, port: int) -> bool:
        if not self._f184:
            return True
        zs = self._zones.get(service) or zones_of(service)
        if zone not in zs:
            return False
        if service in BOTH:
            return port == (8081 if zone == "work" else 8080)
        return True

    def _log(self, caller, zone, service, port, method, path, outcome, status=None) -> None:
        self.calls.append(Call(caller, zone, service, port, method, path, outcome, status))

    def _deliver(self, caller, zone, method, url, headers, body, timeout):
        u = urlsplit(url)
        service, port = u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
        path = u.path
        path_qs = u.path + (("?" + u.query) if u.query else "")
        be = self._services.get((service, port))
        if be is None:
            self._log(caller, zone, service, port, method, path, "refused")
            raise Unavailable("ConnectionRefused")
        if not self._reachable(zone, service, port):
            self._log(caller, zone, service, port, method, path, "no_route")
            raise Unavailable("NoRoute")
        lose = False
        for f in self.faults:
            if not f.matches(service, port, method, path, caller):
                continue
            f.hits += 1
            if f.kind == "down":
                self._log(caller, zone, service, port, method, path, "down")
                raise Unavailable("ConnectionRefused")
            if f.kind == "delay":
                if f.seconds > (timeout or self.default_timeout_s):
                    if self.clock is not None:
                        self.clock.sleep(timeout or self.default_timeout_s)
                    self._log(caller, zone, service, port, method, path, "delayed_timeout")
                    raise Unavailable("Timeout")
                if self.clock is not None:
                    self.clock.sleep(f.seconds)
            elif f.kind == "respond":
                raw = _json.dumps(f.body).encode() if f.body is not None else b""
                self._log(caller, zone, service, port, method, path, "faked", f.status)
                return f.status, {"Content-Type": "application/json"}, raw
            elif f.kind == "lose":
                lose = True
        status, hdrs, raw = be(method, path_qs, headers, body, timeout, url)
        if lose:
            self._log(caller, zone, service, port, method, path, "lost", status)
            raise Unavailable("Timeout")
        self._log(caller, zone, service, port, method, path, "delivered", status)
        return status, hdrs, raw

    # ---- 見る ----
    def calls_to(self, service: str, *, outcome: str | None = None) -> list[Call]:
        return [c for c in self.calls if c.service == service and (outcome is None or c.outcome == outcome)]
