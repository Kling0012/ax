"""timer-runner の本体。外との接点（Services 相当）は引数で渡す。試験は偽を渡す。"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from axcommon import ids, timeutil

from runtime.subloop import schedule
from runtime.subloop.store import Store

log = logging.getLogger("ax.timer_runner")
ACTIONS = ("ledger.recheck", "control.release", "plugin.run_now")


@dataclass
class JobContext:
    """定期ジョブのハンドラに渡す文脈。clients は、そのプラグインのトークンを付けた窓口だけ。"""
    plugin_id: str
    manifest: dict
    run_id: str
    planned_at: str
    clients: dict[str, Any]
    emit: Callable[..., Any]
    now: Callable[[], Any] = timeutil.now
    store: Any = None


@dataclass
class TimerRunner:
    plugin_id: str
    store: Store
    sink: Any
    handlers: dict[str, Callable[[JobContext], Awaitable[dict | None]]]   # 定期ジョブ: プラグイン ID → run(ctx)
    manifests: dict[str, dict]                                              # kind=timer の登録済みプラグインの manifest
    client_factory: Callable[[str, str], Any]                               # (プラグイン ID, 部品名) → そのプラグインのトークンを付けた窓口
    timer_source: Any = None                                                # list_timers() / mark(timer_id, state, result)。None なら期限付きタイマーは扱わない
    actions: dict[str, Callable[[dict], Awaitable[dict | None]]] = field(default_factory=dict)
    mode: str = "run"
    revision: int = 0
    _running: set = field(default_factory=set)
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    _timers_undefined: bool = False
    _fresh_effective: set[str] = field(default_factory=set)
    sink_factory: Callable[[str], Any] | None = None
    require_effective: bool = False
    extra_clients: dict[str, dict[str, Any]] = field(default_factory=dict)

    def set_effective(self, plugin_id: str, effective: dict) -> None:
        if effective.get("mode") not in ("run", "collect_only", "poll_paused", "draining") or type(effective.get("revision")) is not int:
            raise ValueError("invalid effective")
        key = f"effective:{plugin_id}"
        current = self.store.get_meta(key, {})
        if effective["revision"] >= current.get("revision", -1):
            self.store.set_meta(key, effective)
            self._fresh_effective.add(plugin_id)
            if plugin_id == self.plugin_id:
                self.apply(effective["mode"], effective["revision"])

    def _sink(self, plugin_id: str) -> Any:
        return self.sink_factory(plugin_id) if self.sink_factory else self.sink

    def __post_init__(self) -> None:
        self.mode = self.store.get_meta("mode", self.mode)
        self.revision = self.store.get_meta("revision", self.revision)

    # ---- 状態 ----
    def apply(self, mode: str | None, revision: int | None) -> bool:
        if mode is not None and mode not in ("run", "collect_only", "poll_paused", "draining"):
            return False
        if revision is not None and (type(revision) is not int or revision < self.revision):
            return False
        if mode is not None:
            self.mode = mode
            self.store.set_meta("mode", mode)
        if revision is not None:
            self.revision = revision
            self.store.set_meta("revision", revision)
        return True

    def status(self) -> dict:
        runs = self.store.runs()
        last = {"run_id": runs[-1]["run_id"], "state": runs[-1]["state"]} if runs else None
        return {"mode": self.mode, "knobs": {}, "running": sorted(self._running), "queued": 0, "last_run": last}

    def _emit_run(self, subject: str, run_id: str, state: str, reason: str | None = None, extra: dict | None = None) -> None:
        try:
            owner = subject if subject in self.manifests else self.plugin_id
            self._sink(owner).emit("run.state", subject, {"state": state, "reason": reason, **(extra or {})}, run_id=run_id, subloop=owner)
        except Exception as e:  # noqa: BLE001
            log.error("事象を書けない: %s", type(e).__name__)

    def _observe_skip(self, pid: str, rid: str, result: str) -> None:
        target = self.manifests.get(pid, {}).get("skip_observation_target")
        if target is None and pid == "hibi-state":
            target = "focus"
        if not target:
            return
        try:
            self._sink(pid).emit("automation.observed", target, {
                "plugin_id": pid, "target_id": target, "as_of": timeutil.now_ts(), "result": result,
            }, run_id=rid, subloop=pid)
        except Exception as e:
            log.error("observation failed: %s", type(e).__name__)

    # ---- 1 回 ----
    async def tick(self) -> None:
        await self._periodic()
        await self._timers()
        await asyncio.sleep(0)  # 定期ジョブを開始してから次の tick を待つ。

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)

    async def close(self) -> None:
        for task in tuple(self._tasks.values()):
            task.cancel()
        await self.drain()

    async def _periodic(self) -> None:
        for pid, man in sorted(self.manifests.items()):
            if pid == self.plugin_id:
                continue
            try:
                await self._schedule_job(pid, man)
            except Exception as e:  # noqa: BLE001  不正な定期ジョブだけを止める。
                log.error("定期ジョブ %s の予定を読めない: %s", pid, type(e).__name__)

    async def _schedule_job(self, pid: str, man: dict) -> None:
        sch = dict(man.get("schedule") or {})
        effective = self.store.get_meta(f"effective:{pid}", {})
        if "every_s" in effective.get("knobs", {}):
            sch["every_s"] = effective["knobs"]["every_s"]
        if pid == "security-watch" and "u_security_time" in effective.get("knobs", {}):
            import re
            value = effective["knobs"]["u_security_time"]
            if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                raise ValueError("invalid security time")
            hour, minute = map(int, value.split(":"))
            sch["cron"] = f"{minute} {(hour - 9) % 24} * * *"
        kind = sch.get("kind")
        if kind not in ("interval", "cron"):
            return
        if kind == "interval" and (type(sch.get("every_s")) is not int or sch["every_s"] <= 0):
            raise ValueError("every_s must be a positive integer")
        key = f"next_due:{pid}"
        next_due = self.store.get_meta(key)
        if pid == "entry-index":
            from axcommon.defaults import Defaults
            defaults = Defaults.load()
            sch["every_s"] = effective.get("knobs", {}).get("every_s", defaults.get("u_entry_interval_s"))
            if not getattr(self, "_entry_delay_initialized", False):
                earliest = timeutil.add_seconds(timeutil.now_ts(), defaults.get("u_entry_start_delay_s"))
                if next_due is None or timeutil.parse_ts(next_due) < timeutil.parse_ts(earliest):
                    next_due = earliest
                self._entry_delay_initialized = True
        dec = schedule.decide(kind, sch, next_due, timeutil.now())
        self.store.set_meta(key, dec.next_due)
        for ts in dec.missed:
            rid = ids.new_ulid()
            self.store.create_run(rid, state="missed", planned_at=ts, trigger="schedule", reason="late")
            self.store.set_run_state(rid, "missed", "late")
            self._emit_run(pid, rid, "missed", "late", {"planned_at": ts})
        if dec.run:
            if pid == "conversation-index" and not self._conversation_enabled():
                await self.run_job(pid, dec.run)
            elif self._tasks.get(pid) is not None and not self._tasks[pid].done():
                rid = ids.new_ulid()
                self.store.create_run(rid, state="missed", planned_at=dec.run, reason="previous_run_active")
                self.store.set_run_state(rid, "missed", "previous_run_active")
                self._emit_run(pid, rid, "missed", "previous_run_active", {"planned_at": dec.run})
                self._observe_skip(pid, rid, "skipped_busy")
            else:
                self._tasks[pid] = asyncio.create_task(self.run_job(pid, dec.run))

    def _conversation_enabled(self) -> bool:
        effective = self.store.get_meta("effective:conversation-index", {})
        knobs = effective.get("knobs") if isinstance(effective, dict) else None
        return isinstance(knobs, dict) and knobs.get("enabled") is True

    async def run_job(self, pid: str, planned_at: str) -> str | None:
        rid = ids.new_ulid()
        if pid == "conversation-index" and not self._conversation_enabled():
            self.store.create_run(rid, state="missed", planned_at=planned_at, trigger="schedule", reason="disabled")
            self.store.set_run_state(rid, "missed", "disabled")
            self._emit_run(pid, rid, "missed", "disabled")
            return rid
        effective = self.store.get_meta(f"effective:{pid}", {})
        mode = self.mode if self.mode != "run" else effective.get("mode", "poll_paused" if self.require_effective else "run")
        collect_handler = self.manifests.get(pid, {}).get("collect_only_handler") is True
        if self.require_effective and self.store.get_meta(f"effective:{self.plugin_id}") is None:
            mode = "collect_only" if collect_handler else "poll_paused"
        if collect_handler and self.require_effective and pid not in self._fresh_effective and mode == "run":
            mode = "collect_only"
        if mode != "run" and not (mode == "collect_only" and collect_handler):
            self.store.create_run(rid, state="missed", planned_at=planned_at, trigger="schedule", reason=f"mode:{mode}")
            self.store.set_run_state(rid, "missed", f"mode:{mode}")
            self._emit_run(pid, rid, "missed", f"mode:{mode}")
            if mode in ("collect_only", "draining") or self.manifests.get(pid, {}).get("skip_observation_target"):
                self._observe_skip(pid, rid, "skipped_mode")
            return None
        if pid in self._running:
            self.store.create_run(rid, state="missed", planned_at=planned_at, trigger="schedule", reason="previous_run_active")
            self.store.set_run_state(rid, "missed", "previous_run_active")
            self._emit_run(pid, rid, "missed", "previous_run_active", {"planned_at": planned_at})
            self._observe_skip(pid, rid, "skipped_busy")
            return None
        handler = self.handlers.get(pid)
        self.store.create_run(rid, state="planned", planned_at=planned_at, trigger="schedule")
        self._emit_run(pid, rid, "planned")
        if handler is None:
            self.store.set_run_state(rid, "failed", "no_handler")
            self._emit_run(pid, rid, "failed", "no_handler")
            return rid
        self._running.add(pid)
        self.store.set_run_state(rid, "running", None)
        self._emit_run(pid, rid, "running")
        try:
            if mode == "collect_only":
                clients = {"collect_only": self.extra_clients.get(pid, {}).get("collect_only")}
            else:
                clients = {name: self.client_factory(pid, name) for name in ("timeline", "ledger", "resources")}
                clients.update(self.extra_clients.get(pid, {}))
            ctx = JobContext(plugin_id=pid, manifest=self.manifests[pid], run_id=rid, planned_at=planned_at, clients=clients,
                             emit=lambda kind, subject, data=None: self._sink(pid).emit(kind, subject, data or {}, run_id=rid, subloop=pid), store=self.store)
            res = await handler(ctx)
            self.store.set_run_state(rid, "completed", None)
            self._emit_run(pid, rid, "completed", None, {"result": (res or {}).get("result") if isinstance(res, dict) else None})
        except asyncio.CancelledError:
            self.store.set_run_state(rid, "cancelled", "shutdown")
            self._emit_run(pid, rid, "cancelled", "shutdown")
            raise
        except Exception as e:  # noqa: BLE001  1 つのジョブの失敗で全体を止めない
            log.error("定期ジョブ %s の失敗: %s", pid, type(e).__name__)
            self.store.set_run_state(rid, "failed", type(e).__name__)
            self._emit_run(pid, rid, "failed", type(e).__name__)
        finally:
            self._running.discard(pid)
        return rid

    # ---- 管理役の期限付きタイマー ----
    def _missed(self, tid: str, planned_at: str, count: int = 1) -> None:
        rid = ids.new_ulid()
        self.store.create_run(rid, state="missed", planned_at=planned_at, trigger="timer", reason="late")
        self.store.set_run_state(rid, "missed", "late")
        self._emit_run(f"timer:{tid}", rid, "missed", "late", {"planned_at": planned_at, "count": count})

    async def _timers(self) -> None:
        if self.timer_source is None or self.mode != "run" or self._timers_undefined:
            return
        now = timeutil.now()
        try:
            timers = self.timer_source.list_timers()
        except NotImplementedError:
            self._timers_undefined = True
            log.warning("期限付きタイマーの接続は未定義（S-7.8/F-18.4）。定期ジョブは継続する")
            return
        except Exception as e:  # noqa: BLE001
            log.warning("timers を読めない: %s", type(e).__name__)
            return
        for t in timers:
            if t.get("state") in ("fired", "expired", "cancelled", "rejected"):
                continue
            tid = t.get("id")
            exp = t.get("expires_at")
            if exp and timeutil.parse_ts(exp) <= now:
                self._mark(tid, "expired", None)  # 寿命を過ぎたタイマーは実行しない（S-7.8）
                continue
            if t.get("action") not in ACTIONS:
                self._mark(tid, "rejected", "action")
                continue
            every = t.get("every_s")
            key = f"timer_next:{tid}"
            nxt = self.store.get_meta(key)
            if nxt == "done":
                continue
            if nxt is None:
                nxt = t.get("at") or (timeutil.format_ts(now) if every else None)
                if nxt is None:
                    self._mark(tid, "rejected", "at_or_every_s")
                    continue
                self.store.set_meta(key, nxt)
            nd = timeutil.parse_ts(nxt)
            if nd > now:
                continue
            late = (now - nd).total_seconds()
            if every:
                k = int(late // every)
                latest_ts = timeutil.add_seconds(timeutil.format_ts(nd), k * every)
                lat = late - k * every
                if k >= 1:
                    self._missed(tid, timeutil.format_ts(nd), k)  # 最新より前の予定は追いかけない
                self.store.set_meta(key, timeutil.add_seconds(latest_ts, every))
                if lat > max(10.0, every * 0.5):
                    self._missed(tid, latest_ts)
                    continue
                await self._fire(t)
            else:
                self.store.set_meta(key, "done")
                if late > schedule.CRON_GRACE_S:
                    self._missed(tid, timeutil.format_ts(nd))
                    self._mark(tid, "missed", None)
                    continue
                await self._fire(t)
                self._mark(tid, "fired", None)

    def _mark(self, tid: str, state: str, result: Any) -> None:
        try:
            self.timer_source.mark(tid, state, result)
        except Exception as e:  # noqa: BLE001
            log.warning("timer の状態を戻せない: %s", type(e).__name__)

    async def _fire(self, t: dict) -> None:
        tid = t["id"]
        rid = ids.new_ulid()
        subject = f"timer:{tid}"
        self.store.create_run(rid, state="planned", planned_at=timeutil.now_ts(), trigger="timer")
        self._emit_run(subject, rid, "planned")
        act = self.actions.get(t["action"])
        if act is None:
            self.store.set_run_state(rid, "failed", "no_action_impl")
            self._emit_run(subject, rid, "failed", "no_action_impl")
            return
        self.store.set_run_state(rid, "running", None)
        self._emit_run(subject, rid, "running")
        try:
            res = await act(t)
            self.store.set_run_state(rid, "completed", None)
            self._emit_run(subject, rid, "completed", None, {"action": t["action"]})
            if isinstance(res, dict) and res.get("fire_result"):
                self._mark(tid, "running" if t.get("every_s") else "fired", res["fire_result"])
        except Exception as e:  # noqa: BLE001
            self.store.set_run_state(rid, "failed", type(e).__name__)
            self._emit_run(subject, rid, "failed", type(e).__name__)


def load_handler(plugin_dir: str | Path) -> Callable | None:
    """plugins/timer/<id>/handler.py の `async def run(ctx)` を読む。無ければ None。"""
    p = Path(plugin_dir) / "handler.py"
    if not p.is_file():
        return None
    name = f"axtimer_{abs(hash(str(p.resolve()))):x}"
    spec = importlib.util.spec_from_file_location(name, p)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        sys.modules.pop(name, None)
        log.error("handler の読み込みに失敗 %s: %s", p.parent.name, type(e).__name__)
        return None
    fn = getattr(mod, "run", None)
    return fn if callable(fn) else None
