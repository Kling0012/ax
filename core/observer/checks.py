"""検査（S-11.2 の表）。1 つの検査は 1 つの関数で、警報（Alert）の列を返す。

どの検査も、例外で落ちても他の検査は続く（Observer.run_once が受ける。D-10.1）。
事象を読む検査は、先に Memory.ingest が 1 周期ぶんの事象をまとめて取り込んだ状態を見る（購読の読み位置が 1 回しか進まないため）。
入力の取り方のうち文書で決まらない物は、この順に決めた（DECISIONS.md の b8、OPEN.md の b8）。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from axcommon import timeutil

from .model import DEST_BACKUP, DEST_HOST, DEST_INSPECTION, DEST_MANAGER, Alert
from .settings import SELF_NAMES, Settings
from .sources import Sources, component_of_service, order_key, parse_ts_or_none

EVENT_KINDS = ("error.classified", "manager.cycle", "manager.decision", "inspection.phase")
MANAGER_NAMES = ("manager", "backup")
# 点検の「減らし込み」の間とみなす phase（S-13.2 の段2〜7）。phase の名前は SPEC.md に列挙が無い（OPEN.md）。
DRAIN_PHASES = frozenset({"drain", "draining", "wait", "review_request", "review", "plan_review"})
UNKNOWN_KEEP_H = 24
WORK_ONLY_PREFIXES = ("sl-",)
WORK_ONLY_SERVICES = frozenset({"timer-runner"})


@dataclass
class Memory:
    """事象から作る、周期をまたぐ状態。再起動後は購読の backfill で直近を取り戻す。"""
    started: datetime
    errors: deque = field(default_factory=deque)  # (ts, component, code)
    last_manager_cycle: datetime | None = None
    last_manager_activity: datetime | None = None  # manager.cycle / manager.decision（manager 名義）
    bad_outputs: int = 0  # 最後の manager.decision 以後の error.classified(bad_output)（manager 名義）
    unknown_pending: dict[str, datetime] = field(default_factory=dict)
    last_inspection: tuple[str, datetime] | None = None

    def ingest(self, events: list[dict], window_s: float, now: datetime) -> None:
        for ev in sorted(events, key=order_key):
            ts = parse_ts_or_none(ev.get("ts")) or now
            kind = ev.get("kind")
            src = ev.get("source") or {}
            comp = src.get("component") if isinstance(src, dict) else None
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            if kind == "error.classified":
                code = str(data.get("code", ""))
                self.errors.append((ts, str(comp), code))
                if data.get("class") == "unknown" and isinstance(ev.get("id"), str):
                    self.unknown_pending[ev["id"]] = ts
                if comp == "manager" and code == "bad_output":
                    self.bad_outputs += 1
            elif kind in ("manager.cycle", "manager.decision"):
                if comp in MANAGER_NAMES:
                    if comp == "manager" and kind == "manager.cycle":
                        if self.last_manager_cycle is None or ts > self.last_manager_cycle:
                            self.last_manager_cycle = ts
                    if comp == "manager" and (self.last_manager_activity is None or ts > self.last_manager_activity):
                        self.last_manager_activity = ts
                    if kind == "manager.decision" and comp == "manager":
                        self.bad_outputs = 0
                    self._mark_handled(ev, data)
            elif kind == "inspection.phase":
                phase = data.get("phase")
                if isinstance(phase, str):
                    self.last_inspection = (phase, ts)
        cutoff = now - timedelta(seconds=window_s)
        while self.errors and self.errors[0][0] < cutoff:
            self.errors.popleft()
        keep = now - timedelta(hours=UNKNOWN_KEEP_H)
        for k in [k for k, t in self.unknown_pending.items() if t < keep]:
            del self.unknown_pending[k]

    def _mark_handled(self, ev: dict, data: dict) -> None:
        """管理役・予備役が unknown の事象を扱った印。cause か、data の event_id / event_ids / handled に事象の ID がある。"""
        ids: set[str] = set()
        if isinstance(ev.get("cause"), str):
            ids.add(ev["cause"])
        if isinstance(data.get("event_id"), str):
            ids.add(data["event_id"])
        for key in ("event_ids", "handled"):
            v = data.get(key)
            if isinstance(v, list):
                ids.update(x for x in v if isinstance(x, str))
        for i in ids:
            self.unknown_pending.pop(i, None)


@dataclass
class Ctx:
    settings: Settings
    sources: Sources
    now: datetime
    memory: Memory
    health: dict[str, dict] | None  # 時系列記録が落ちているとき None
    registered: dict[str, float]
    watch_signal: Callable[[], list[dict]] | None = None
    _plugins: dict | None = None
    _controls: dict | None = None
    _jobs: dict | None = None

    def plugins(self) -> dict[str, dict]:
        if self._plugins is None:
            self._plugins = self.sources.plugins()
        return self._plugins

    def controls(self) -> dict[str, dict]:
        if self._controls is None:
            self._controls = self.sources.controls()
        return self._controls

    def jobs(self) -> dict[str, dict]:
        if self._jobs is None:
            self._jobs = self.sources.jobs()
        return self._jobs


def expected_components(ctx: Ctx) -> dict[str, str]:
    """beat を期待する部品 -> compose のサービス名。登録済みのサービスと、投影にいる部品。自分と司令塔は除く。"""
    out: dict[str, str] = {}
    for svc in ctx.registered:
        comp = component_of_service(svc)
        out[comp] = svc
    for comp in (ctx.health or {}):
        out.setdefault(comp, comp)
    for name in list(out):
        if name in SELF_NAMES or name in ctx.settings.commander_units or out[name] in ctx.settings.commander_units:
            del out[name]
    return out


def _liveness_destinations(ctx: Ctx, comp: str, svc: str) -> tuple[str, ...]:
    """プラグインなら管理役、中核ならホスト見張り（再起動）＋管理役（S-11.2）。"""
    try:
        plugin_ids = set(ctx.plugins())
    except Exception:  # noqa: BLE001  plugins 投影が読めなければ、サービス名で判断する
        plugin_ids = set()
    is_plugin = comp in plugin_ids or (svc.startswith("sl-") and comp not in ctx.settings.core_services)
    if is_plugin:
        return (DEST_MANAGER,)
    return (DEST_HOST, DEST_MANAGER)


def check_liveness(ctx: Ctx) -> list[Alert]:
    """ある部品の health.beat が stale_beats 回分ない。時系列記録が落ちているときは各部品の /healthz を直接見る（D-10.1）。"""
    s = ctx.settings
    alerts: list[Alert] = []
    exp = expected_components(ctx)
    now_epoch = ctx.now.timestamp()
    if ctx.health is None:
        for comp, svc in sorted(exp.items()):
            if svc.startswith(WORK_ONLY_PREFIXES) or svc in WORK_ONLY_SERVICES:
                continue  # 作業側の網（ax-work）の部品には ax-core から /healthz が届かない。時系列記録が戻るまで判定しない
            if not ctx.sources.probe(comp):
                alerts.append(Alert("liveness", comp, _liveness_destinations(ctx, comp, svc), "high", "healthz が通らない（時系列記録が使えない）"))
        # 時系列記録そのものが落ちている
        alerts.append(Alert("liveness", "timeline", (DEST_HOST, DEST_MANAGER), "high", "時系列記録に届かない"))
        return alerts
    for comp, svc in sorted(exp.items()):
        row = ctx.health.get(comp)
        last = parse_ts_or_none(row.get("last_beat_at")) if row else None
        if last is None:
            # 一度も beat が無い: 登録の時刻と、オブザーバーの起動の遅い方から数える
            ref = max(ctx.registered.get(svc, 0.0), ctx.memory.started.timestamp())
            age = now_epoch - ref
        else:
            age = (ctx.now - last).total_seconds()
        if age > s.stale_s:
            alerts.append(Alert("liveness", comp, _liveness_destinations(ctx, comp, svc), "high",
                                f"beat が {int(age)} 秒ない（閾値 {int(s.stale_s)} 秒）"))
    return alerts


def check_watch(ctx: Ctx) -> list[Alert]:
    """各サブループの見張り用ノードが出した警報。見張り用ノードが何を書くかが文書にない（OPEN.md）。差し込み口 watch_signal があるときだけ動く。"""
    if ctx.watch_signal is None:
        raise NotImplementedError("watch: 見張り用ノードの警報の形が文書で決まっていない（OPEN.md の b8）")
    out = []
    for sig in ctx.watch_signal() or []:
        target = str(sig.get("target") or sig.get("subloop") or "unknown")
        out.append(Alert("watch", target, (DEST_MANAGER,), str(sig.get("severity") or "warn"), str(sig.get("detail") or "")[:200]))
    return out


def check_expired_pending(ctx: Ctx) -> list[Alert]:
    """expired_pending が expired_grace_min を超えた。宛先は出した者。出した者が管理役なら予備役（S-11.2）。"""
    out = []
    grace = timedelta(minutes=ctx.settings.expired_grace_min)
    for cid, c in sorted(ctx.controls().items()):
        if c.get("state") != "expired_pending":
            continue
        exp = parse_ts_or_none(c.get("expires_at"))
        if exp is None or ctx.now - exp <= grace:
            continue
        issuer = str(c.get("issued_by") or "manager")
        dest = DEST_BACKUP if issuer == "manager" else issuer
        out.append(Alert("expired_pending", cid, (dest,), "warn", f"期限切れから {int((ctx.now - exp).total_seconds() // 60)} 分、出した者 {issuer} が決めていない"))
    return out


def check_lag(ctx: Ctx) -> list[Alert]:
    """時系列記録の購読の遅れ（health.beat の lag_s）が lag_alert_s を超えた。"""
    out = []
    for comp, row in sorted((ctx.health or {}).items()):
        lag = row.get("lag_s")
        if isinstance(lag, (int, float)) and not isinstance(lag, bool) and lag > ctx.settings.lag_alert_s:
            out.append(Alert("lag", comp, (DEST_MANAGER,), "warn", f"購読の遅れ {int(lag)} 秒"))
    return out


def check_starve(ctx: Ctx) -> list[Alert]:
    """役割の待ちの列が starve_min 分進まない。jobs 投影で、役割ごとに queued の行があり、その役割のどの行も starve_min 分更新されていない。"""
    by_role: dict[str, list[dict]] = {}
    for v in ctx.jobs().values():
        role = v.get("role")
        if isinstance(role, str) and role:
            by_role.setdefault(role, []).append(v)
    out = []
    limit = timedelta(minutes=ctx.settings.starve_min)
    for role, rows in sorted(by_role.items()):
        if not any(r.get("state") == "queued" for r in rows):
            continue
        last = max((t for t in (parse_ts_or_none(r.get("last_ts")) for r in rows) if t), default=None)
        if last is not None and ctx.now - last > limit:
            out.append(Alert("starve", role, (DEST_MANAGER,), "warn", f"役割 {role} の待ちが {int((ctx.now - last).total_seconds() // 60)} 分進まない"))
    return out


def check_repeat(ctx: Ctx) -> list[Alert]:
    """同じ error.classified（部品と code の組）が 1 時間に repeat_threshold 回を超えた。"""
    counts: dict[tuple[str, str], int] = {}
    for _ts, comp, code in ctx.memory.errors:
        counts[(comp, code)] = counts.get((comp, code), 0) + 1
    return [Alert("repeat", f"{comp}:{code}", (DEST_MANAGER,), "warn", f"{n} 回/時間")
            for (comp, code), n in sorted(counts.items()) if n > ctx.settings.repeat_threshold]


def _manager_expected(ctx: Ctx) -> bool:
    """管理役が動いているはずか。登録済み、または beat が見えている間だけ「不調」を判定する（起動前に予備役を呼ばない）。"""
    return "manager" in ctx.registered or "manager" in (ctx.health or {})


def check_manager_trouble(ctx: Ctx) -> list[Alert]:
    """管理役の不調 -> 予備役。(1) manager.cycle が manager_silent_min 分ない (2) 判断が manager_bad_streak 回続けて捨てられた (3) unknown が unknown_unhandled_min 分扱われない。"""
    if not _manager_expected(ctx):
        return []
    s = ctx.settings
    out = []
    last = ctx.memory.last_manager_cycle
    ref = last if last is not None else ctx.memory.started
    silent = ctx.now - ref
    if silent > timedelta(minutes=s.manager_silent_min):
        out.append(Alert("manager_trouble", "silent", (DEST_BACKUP,), "high", f"manager.cycle が {int(silent.total_seconds() // 60)} 分ない"))
    if ctx.memory.bad_outputs >= s.manager_bad_streak:
        out.append(Alert("manager_trouble", "bad_streak", (DEST_BACKUP,), "high", f"判断が {ctx.memory.bad_outputs} 回続けて捨てられた"))
    if ctx.memory.unknown_pending:
        oldest = min(ctx.memory.unknown_pending.values())
        if ctx.now - oldest > timedelta(minutes=s.unknown_unhandled_min):
            out.append(Alert("manager_trouble", "unknown_unhandled", (DEST_BACKUP,), "high",
                             f"unknown の事象 {len(ctx.memory.unknown_pending)} 件が {int((ctx.now - oldest).total_seconds() // 60)} 分扱われない"))
    return out


def check_drain_silent(ctx: Ctx) -> list[Alert]:
    """点検の減らし込み中に、管理役が drain_silent_min 分動かない -> 点検係へ「再開」を指示（S-13.3）。"""
    insp = ctx.memory.last_inspection
    if insp is None or insp[0] not in DRAIN_PHASES:
        return []
    phase, since = insp
    act = ctx.memory.last_manager_activity
    ref = max(since, act) if act is not None else since
    if ctx.now - ref > timedelta(minutes=ctx.settings.drain_silent_min):
        return [Alert("drain_silent", "inspection", (DEST_INSPECTION,), "warn", f"点検の {phase} の間、管理役が {int((ctx.now - ref).total_seconds() // 60)} 分動かない。再開")]
    return []


DEFAULT_CHECKS: tuple[tuple[str, Callable[[Ctx], list[Alert]]], ...] = (
    ("liveness", check_liveness),
    ("watch", check_watch),
    ("expired_pending", check_expired_pending),
    ("lag", check_lag),
    ("starve", check_starve),
    ("repeat", check_repeat),
    ("manager_trouble", check_manager_trouble),
    ("drain_silent", check_drain_silent),
)
