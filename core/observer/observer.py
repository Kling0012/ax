"""Observer: observe_s ごとに検査を全部回す（D-10.1）。1 つの検査・取り込みの失敗で他を止めない。"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

from axcommon import timeutil
from axcommon.client import ApiError, LedgerClient, TimelineClient, Unavailable
from axcommon.events import EventSink

from . import COMPONENT, hostreq
from .checks import DEFAULT_CHECKS, EVENT_KINDS, Ctx, Memory
from .cutover import CutoverIngester
from .dedup import DedupTable
from .feed import EventFeed
from .hostlog import HostLogSync
from .model import DEST_BACKUP, DEST_HOST, DEST_INSPECTION, Alert
from .settings import Settings
from .sources import Sources, component_of_service

log = logging.getLogger("observer")

Check = Callable[[Ctx], "list[Alert]"]
Hook = Callable[[Alert], Any]


class Observer:
    def __init__(self, settings: Settings, timeline: TimelineClient, sink: EventSink, *, ledger: LedgerClient | None = None,
                 sources: Sources | None = None, redact: Callable[[Any], Any] | None = None):
        self.s = settings
        self.timeline, self.sink = timeline, sink
        self.sources = sources or Sources(timeline, settings.state_dir)
        data = Path(settings.data_dir)
        self.dedup = DedupTable(data / "dedup.json", settings.alert_dedup_min)
        self.feed = EventFeed(self.sources, data / "cursors.json", settings.feed_backfill)
        self.memory = Memory(started=timeutil.now())
        self.cutover = CutoverIngester(settings.state_dir, data, timeline, sink, redact)
        self.hostlog = HostLogSync(settings.state_dir, data, timeline, sink, ledger)
        self.checks: list[tuple[str, Check]] = list(DEFAULT_CHECKS)
        self.hooks: dict[str, Hook] = {}  # 宛先 -> 呼び出し（backup は B10 の予備役、inspection は点検係。ext が差し込む）
        self.watch_signal: Callable[[], list[dict]] | None = None
        self.runs = 0
        self.last_run_at: str | None = None
        self.unimplemented: set[str] = set()
        self.failed: dict[str, int] = {}
        self.timeline_down = False
        self.alerts_total = 0
        self._lock = threading.Lock()

    # --- 拡張の差し込み口（ext/<束の ID>.py の register(app) が app.state.observer に対して使う） ---
    def add_check(self, name: str, fn: Check) -> None:
        self.checks.append((name, fn))

    def set_hook(self, destination: str, fn: Hook) -> None:
        self.hooks[destination] = fn

    # --- /state/observer.beat（ホスト見張りが読む。S-11.1） ---
    def write_beat(self) -> bool:
        p = Path(self.s.state_dir) / "observer.beat"
        try:
            # ファイル単位の rw bind。inode を変えず、ホストの読み手と共有する。
            with p.open("w", encoding="utf-8") as f:
                f.write(timeutil.now_ts() + "\n")
                f.flush()
            return True
        except OSError:
            return False

    # --- 1 周期 ---
    def run_once(self) -> dict:
        with self._lock:
            return self._run_once()

    def _run_once(self) -> dict:
        self.runs += 1
        self.write_beat()
        now = timeutil.now()
        self.last_run_at = timeutil.format_ts(now)
        result: dict[str, Any] = {"alerts": [], "errors": []}
        for name, fn in (("cutover", self.cutover.run), ("hostlog", self.hostlog.run)):
            try:
                result[name] = fn()
            except Exception as e:  # noqa: BLE001  取り込みの失敗は他を止めない
                self.failed[name] = self.failed.get(name, 0) + 1
                result["errors"].append(f"{name}:{type(e).__name__}")
        health, tail = None, 0
        try:
            health, tail = self.sources.health()
            self.timeline_down = False
        except Unavailable:
            self.timeline_down = True
        except Exception as e:  # noqa: BLE001
            self.timeline_down = True
            result["errors"].append(f"health:{type(e).__name__}")
        if health is not None:
            self._ingest_events(tail, now, result)
        ctx = Ctx(settings=self.s, sources=self.sources, now=now, memory=self.memory, health=health,
                  registered=self.sources.registered(), watch_signal=self.watch_signal)
        alerts: list[Alert] = []
        for name, fn in self.checks:
            try:
                alerts.extend(fn(ctx))
            except NotImplementedError:
                self.unimplemented.add(name)
            except Unavailable:
                self.timeline_down = True  # 時系列記録を要する検査はこの周期は飛ばす
            except Exception as e:  # noqa: BLE001
                self.failed[name] = self.failed.get(name, 0) + 1
                result["errors"].append(f"{name}:{type(e).__name__}")
        for a in alerts:
            try:
                if self.dedup.allow(a.check, a.target, now):
                    self.deliver(a)
                    result["alerts"].append((a.check, a.target))
            except Exception as e:  # noqa: BLE001
                self.failed["deliver"] = self.failed.get("deliver", 0) + 1
                result["errors"].append(f"deliver:{type(e).__name__}")
        try:
            self.sink.flush()
        except Exception:  # noqa: BLE001
            pass
        return result

    def _ingest_events(self, tail: int, now, result: dict) -> None:
        events: list[dict] = []
        for kind in EVENT_KINDS:
            try:
                events.extend(self.feed.poll(kind, tail))
            except Unavailable:
                self.timeline_down = True
                break
            except Exception as e:  # noqa: BLE001
                result["errors"].append(f"feed:{type(e).__name__}")
        self.memory.ingest(events, self.s.repeat_window_min * 60, now)

    # --- 警報の配送（S-11.2 の宛先） ---
    def deliver(self, a: Alert) -> None:
        self.alerts_total += 1
        for dest in a.destinations:
            self.sink.emit("health.alert", f"{a.check}:{a.target}",
                           {"check": a.check, "target": a.target, "severity": a.severity, "destination": dest, "detail": a.detail})
            if dest == DEST_HOST:
                self._request_restart(a.target)
            elif dest == DEST_BACKUP:
                self._call_backup(a)
            else:
                hook = self.hooks.get(dest)
                if hook is not None:
                    self._safe_hook(hook, a)

    def _request_restart(self, component: str) -> None:
        """中核の部品の起動し直しを、ホスト見張りへ要求ファイルで頼む（S-11.6）。登録済みのサービスだけ。重複して書かない。"""
        reg = self.sources.registered()
        svc = next((s for s in reg if component_of_service(s) == component and not s.startswith("sl-")), None)
        if svc is None:
            return
        d = Path(self.s.state_dir) / "host-requests"
        if ("restart", svc) in hostreq.pending(d):
            return
        try:
            hostreq.write_request(d, "restart", svc)
        except (OSError, ValueError):
            self.failed["host_request"] = self.failed.get("host_request", 0) + 1

    def _call_backup(self, a: Alert) -> None:
        """予備役（S-11.3）。1 backup.activated を書き、2 予備役を 1 回起動する。起動は B10 の ext が set_hook("backup") で差し込む。"""
        hook = self.hooks.get(DEST_BACKUP)
        if hook is None:
            self.failed["backup_unavailable"] = self.failed.get("backup_unavailable", 0) + 1
            return
        from dataclasses import replace

        event = self.sink.emit("backup.activated", f"{a.check}:{a.target}", {"reason": f"{a.check}:{a.target} {a.detail}"[:300]})
        self._safe_hook(hook, replace(a, activation_event=event["id"]))

    def _safe_hook(self, hook: Hook, a: Alert) -> None:
        try:
            hook(a)
        except Exception as e:  # noqa: BLE001  予備役・点検係の呼び出しの失敗でオブザーバーを止めない
            self.failed["hook"] = self.failed.get("hook", 0) + 1
            log.error("hook の失敗: %s", type(e).__name__)

    # --- /healthz に足す物 ---
    def health_extra(self) -> dict:
        return {"runs": self.runs, "last_run_at": self.last_run_at, "timeline_down": self.timeline_down,
                "alerts_total": self.alerts_total, "unimplemented_checks": sorted(self.unimplemented),
                "failed": dict(self.failed), "hooks": sorted(self.hooks)}
