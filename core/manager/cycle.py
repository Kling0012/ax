"""管理役の 1 回の周回（S-10.2、D-9.1、D-9.2）。

Manager.fetch()      時系列記録の購読 → 受信箱（契機の事象だけ）。届かなくても止めない。
Manager.due()        周回を始める契機: 受信箱に契機の事象がある（event）か、manager_tick_min を過ぎた（tick）。
Manager.run_cycle()  順番どおりに 1 周回する（必須の順番）。
  1 自分の命令（expired_pending と期限が近い物）。最初に行う。解除か延長を決める。
  2 unknown の事象、3 崩れた所の緩和（除外・mode・cancel・タイマー）、5 check_at を過ぎた parked の見直し … 1 回の LLM 呼び出し
  4 resource.repeat_failure から恒久修繕の ToDo（機械的。同じ資源と code の ToDo が既にあれば作らない。D-9.3）
  （点検の見直し inspection.phase=review_request は review.py）
  6 manager.cycle を書く。
1 つの段の失敗は他の段を止めない（fail-soft）。判断の時間切れ・形の誤りは事象を台帳へ出して次へ進む（D-9.2）。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from axcommon import ids, timeutil
from axcommon.client import ApiError, Unavailable
from axcommon.events import EventSink

from . import COMPONENT, TRIGGER_KINDS, extschemas, policy, prompt as prompts, summary
from .actions import Executor, propose_rejections
from .config import ManagerConfig
from .llm import DecisionTimeout, LLMUnavailable, parse_json
from .store import Store

log = logging.getLogger("ax.manager")

# 同じ判断の中での実行順（S-10.2: 命令の整理 → 緩和 → ToDo → 台帳）。数字が小さい物から
ORDER = {"control.release": 0, "control.renew": 0, "control.issue": 1, "timer.create": 2, "ledger.eject": 3, "ledger.add": 4, "noop": 5}
OPEN_TODO_STATES = ("todo", "running", "review", "waiting_user", "parked")


def is_trigger(ev: dict) -> bool:
    """S-10.1 の契機。"""
    kind = ev.get("kind")
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    if kind == "error.classified":
        return data.get("class") == "unknown"
    if kind == "health.alert":
        return data.get("destination") == "manager"
    if kind == "control.expired_pending":
        return ((data.get("command") or {}).get("issued_by")) == "manager"
    if kind == "resource.repeat_failure":
        return True
    if kind == "inspection.phase":
        return data.get("phase") == "review_request"
    if kind == "job.state":
        return data.get("state") == "blocked"
    return False


class Outcome:
    def __init__(self, status: str, **kw):
        self.status = status               # ok / timeout / unavailable / bad
        self.reason: str = kw.get("reason", "")
        self.job_id = kw.get("job_id")
        self.model = kw.get("model")
        self.actions: list[dict] = kw.get("actions", [])
        self.results: list[dict] = kw.get("results", [])
        self.rejected: list[dict] = kw.get("rejected", [])

    @property
    def effective(self) -> list[dict]:
        """noop 以外で、実行できた（API が受けた）物。"""
        return [r for r in self.results if r.get("ok") and r["type"] != "noop"]


class Manager:
    def __init__(self, clients, store: Store, sink: EventSink, cfg: ManagerConfig, llm, *, outbox_dir: str | Path,
                 redact: Callable[[Any], Any] = summary.identity, models_for_role: Callable[[], list[dict]] | None = None):
        self.c, self.store, self.sink, self.cfg, self.llm = clients, store, sink, cfg, llm
        self.redact = redact
        self.exec = Executor(clients, outbox_dir, parked_eject_days=cfg.parked_eject_days, host=cfg.host)
        self._models_for_role = models_for_role
        self.timeline_ok = True
        self.last_cycle_at: str | None = store.meta("last_cycle_at")
        self.last_tick_at: str | None = store.meta("last_tick_at")
        self.cycles = 0

    def now(self):
        return timeutil.now()

    # ---------------- 購読 ----------------
    def fetch(self, wait: float = 0.0) -> int:
        """契機の事象を受信箱に足す。足した件数。時系列記録に届かなければ 0（止めない。S-2.2）。"""
        try:
            resp = self.c.timeline.get_events(after=self.store.cursor, kind=",".join(TRIGGER_KINDS), limit=200, wait=wait or None) or {}
        except (Unavailable, ApiError):
            self.timeline_ok = False
            return 0
        self.timeline_ok = True
        added = 0
        for ev in resp.get("events") or []:
            if is_trigger(ev) and self.store.add_event(ev):
                added += 1
        nxt = resp.get("next_after")
        if isinstance(nxt, int):
            self.store.set_cursor(nxt)
        elif resp.get("events"):
            self.store.set_cursor(max(int(e.get("seq") or 0) for e in resp["events"]))
        return added

    def due(self) -> str | None:
        if self.store.pending_count() > 0:
            return "event"
        last = self.last_tick_at
        if last is None or self.now() - timeutil.parse_ts(last) >= timedelta(minutes=self.cfg.tick_min):
            return "tick"
        return None

    # ---------------- 周回 ----------------
    def run_cycle(self, trigger: str = "tick") -> dict:
        cycle_id = ids.new_ulid()
        t0 = self.now()
        st: dict[str, Any] = {"cycle_id": cycle_id, "trigger": trigger, "handled": [], "ejected": [], "decisions": 0, "discarded": 0,
                              "executed": 0, "rejected": 0, "errors": [], "model": None}
        if trigger == "tick":
            self.last_tick_at = timeutil.format_ts(t0)
            self.store.set_meta("last_tick_at", self.last_tick_at)
        ctx: dict[str, Any] = {}
        steps = [("flush", lambda: self._flush(st)), ("context", lambda: ctx.update(self._gather(st))),
                 ("commands", lambda: self._step_commands(ctx, st)), ("inbox", lambda: self._step_inbox(ctx, st)),
                 ("repairs", lambda: self._step_repairs(ctx, st)), ("review", lambda: self._step_review(ctx, st))]
        for name, fn in steps:
            try:
                fn()
            except Exception as e:  # noqa: BLE001  1 つの段の失敗で周回を止めない（fail-soft）
                log.error("段 %s が失敗: %s", name, type(e).__name__)
                st["errors"].append(f"{name}:{type(e).__name__}")
        self.cycles += 1
        self.last_cycle_at = timeutil.format_ts(self.now())
        self.store.set_meta("last_cycle_at", self.last_cycle_at)
        # 6 manager.cycle
        self.sink.emit("manager.cycle", COMPONENT, {
            "cycle_id": cycle_id, "trigger": trigger, "model": st["model"], "decisions": st["decisions"], "discarded": st["discarded"],
            "executed": st["executed"], "rejected": st["rejected"], "handled": st["handled"], "ejected": st["ejected"], "errors": st["errors"],
            "inbox": self.store.counts(), "elapsed_s": round((self.now() - t0).total_seconds(), 3)})
        return st

    # ---------------- 下ごしらえ ----------------
    def _flush(self, st) -> None:
        for fn in (self.sink.flush, self.exec.flush_ledger, self.exec.flush_memory):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    def _gather(self, st) -> dict:
        ctx: dict[str, Any] = {"control_ok": True, "plugins": {}, "own": {}, "plugin_list": []}
        try:
            plist = (self.c.control.plugins() or {}).get("plugins") or []
            ctx["plugin_list"] = plist
            ctx["plugins"] = {p["id"]: dict(p) for p in plist if isinstance(p, dict) and "id" in p}
            live = []
            for state in ("expired_pending", "active"):
                live.extend(self.c.control.commands(state=state))
            ctx["live_commands"] = live
            ctx["own"] = {c["id"]: c for c in live if c.get("issued_by") == "manager"}
        except (Unavailable, ApiError):
            ctx["control_ok"] = False
        try:
            ctx["resources"] = self.c.resources.models() if self.c.resources else []
        except (Unavailable, ApiError):
            ctx["resources"] = []
        return ctx

    def _policy_ctx(self, ctx: dict) -> policy.PolicyContext:
        return policy.PolicyContext(plugins=ctx["plugins"], own_commands=ctx["own"], control_max_lease_min=self.cfg.control_max_lease_min,
                                    control_max_renewals=self.cfg.control_max_renewals, timer_max_days=self.cfg.timer_max_days,
                                    plugins_known=ctx.get("control_ok", True), now=self.now())

    def _load_manifests(self, ctx: dict, actions: list[dict]) -> None:
        """set_knob の検査に要る manifest（knob の who）を、対象のプラグインだけ引く。"""
        for a in actions:
            if a.get("type") == "control.issue" and (a.get("command") or {}).get("op") == "set_knob":
                t = a["command"].get("target")
                if t in ctx["plugins"] and "manifest" not in ctx["plugins"][t]:
                    try:
                        view = self.c.control.plugin(t) or {}
                        if view.get("manifest"):
                            ctx["plugins"][t]["manifest"] = view["manifest"]
                            ctx["plugins"][t]["user_set_knobs"] = view.get("user_set_knobs") or {}
                    except (Unavailable, ApiError):
                        pass

    # ---------------- 判断 1 回 ----------------
    def _decide(self, *, step: str, prompt_text: str, sensitivity: str, ctx: dict, st: dict, allow: tuple | None = None,
                project: str | None = None) -> Outcome:
        try:
            r = self.llm.ask(self.redact(prompt_text), sensitivity=sensitivity, deadline_s=self.cfg.decision_s, project=project)
        except DecisionTimeout as e:
            return Outcome("timeout", reason=str(e))
        except LLMUnavailable as e:
            return Outcome("unavailable", reason=e.reason)
        st["model"] = r.model or st["model"]
        obj = parse_json(r.text)
        errs = ["返答に JSON オブジェクトが無い"] if obj is None else extschemas.errors("manager-decision", obj)
        if errs:
            return Outcome("bad", reason=errs[0][:200], job_id=r.job_id, model=r.model)
        actions = obj["actions"]
        self._load_manifests(ctx, actions)
        ok, rejected = policy.filter_actions(actions, self._policy_ctx(ctx))
        if allow is not None:
            keep = []
            for i, a in ok:
                if a["type"] in allow:
                    keep.append((i, a))
                else:
                    rejected.append({"index": i, "type": a["type"], "code": "step_scope", "message": "この段で使える操作でない"})
            ok = keep
        results = [{"index": i, **self.exec.run(a)} for i, a in sorted(ok, key=lambda x: (ORDER.get(x[1]["type"], 9), x[0]))]
        try:
            propose_rejections(self.exec, ctx, actions, rejected, results)
        except Exception as exc:
            log.warning("proposal creation unavailable: %s", type(exc).__name__)
        return Outcome("ok", job_id=r.job_id, model=r.model, actions=actions, results=results, rejected=rejected)

    def _record_ok(self, step: str, out: Outcome, st: dict, event_ids: list[str], handled: list[str], trigger: str,
                   extra: dict | None = None) -> dict:
        st["decisions"] += 1
        st["executed"] += len(out.effective)
        st["rejected"] += len(out.rejected)
        data = {"cycle_id": st["cycle_id"], "step": step, "trigger": trigger, "model": out.model, "job_id": out.job_id,
                "event_ids": event_ids, "handled": handled,
                "actions": [{"type": a.get("type"), "reason": summary.one_line(a.get("reason", ""), 120)} for a in out.actions],
                "executed": out.results, "rejected": out.rejected}
        data.update(extra or {})
        ev = self.sink.emit("manager.decision", COMPONENT, data, cause=event_ids[0] if event_ids and len(event_ids[0]) == 26 else None)
        self.store.record_decision(st["cycle_id"], step, ev["ts"], {k: data[k] for k in ("actions", "executed", "rejected", "event_ids")},
                                   job_id=out.job_id, model=out.model)
        self.store.set_meta("bad_streak", "0")
        return ev

    def _record_failure(self, step: str, out: Outcome, st: dict, events: list[dict]) -> None:
        """時間切れ・形の誤りの記録（unavailable はここへ来ない）。"""
        first = events[0]["id"] if events and isinstance(events[0].get("id"), str) and len(events[0]["id"]) == 26 else None
        if out.status == "bad":
            st["discarded"] += 1
            self.store.set_meta("bad_streak", str(int(self.store.meta("bad_streak", "0") or 0) + 1))
            self.sink.emit("error.classified", COMPONENT, {"class": "artifact.bug", "code": "bad_output",
                                                           "message": summary.one_line(f"{step}: {out.reason}", 200)}, cause=first)
        else:
            self.sink.emit("error.classified", COMPONENT, {"class": "system.expected", "code": "manager_timeout",
                                                           "message": f"{step}: 判断が {self.cfg.decision_s:g} 秒を超えた"}, cause=first)

    # ---------------- 1 自分の命令 ----------------
    def _step_commands(self, ctx: dict, st: dict) -> None:
        if not ctx["control_ok"]:
            return
        now = self.now()
        near = now + timedelta(minutes=self.cfg.near_expiry_min)
        due = []
        for c in ctx["own"].values():
            exp = c.get("expires_at")
            if c.get("state") == "expired_pending" or (exp and timeutil.parse_ts(exp) <= near):
                due.append(c)
        due.sort(key=lambda c: (c.get("state") != "expired_pending", c.get("expires_at") or ""))
        if due:
            text = prompts.build_commands_prompt(now=timeutil.format_ts(now), due=[self._cmd_view(c) for c in due],
                                                 lease_min=self.cfg.control_max_lease_min, max_renewals=self.cfg.control_max_renewals)
            out = self._decide(step="commands", prompt_text=text, sensitivity="private", ctx=ctx, st=st,
                               allow=("control.release", "control.renew", "noop"))
            if out.status == "ok":
                self._record_ok("commands", out, st, [], [], st["trigger"], {"command_ids": [c["id"] for c in due]})
            elif out.status in ("bad", "timeout"):
                self._record_failure("commands", out, st, [])
            # 命令は残る。決められなければオブザーバーが期限切れの警報を出し、予備役へ回る（S-11.2）
        # control.expired_pending の事象: 命令がもう expired_pending でなければ片付いた
        pending_ids = {c["id"] for c in (self._refresh_own(ctx)).values() if c.get("state") == "expired_pending"}
        done, keep = [], []
        for ev in self.store.pending():
            if ev["kind"] != "control.expired_pending":
                continue
            cid = ((ev.get("data") or {}).get("command") or {}).get("id")
            (keep if cid in pending_ids else done).append(ev["id"])
        self.store.mark(done, "handled", cycle_id=st["cycle_id"])
        st["handled"] += done
        if keep:
            counts = self.store.bump_attempts(keep)
            over = [i for i in keep if counts[i] >= self.cfg.attempts_max]
            self._eject_ids(over, "expired_pending_unresolved", st)

    def _cmd_view(self, c: dict) -> dict:
        return {k: c.get(k) for k in ("id", "target", "op", "args", "reason", "state", "issued_by", "issued_at", "expires_at", "on_expiry", "renewals")}

    def _refresh_own(self, ctx: dict) -> dict:
        try:
            live = []
            for state in ("expired_pending", "active"):
                live.extend(self.c.control.commands(state=state))
            ctx["live_commands"] = live
            ctx["own"] = {c["id"]: c for c in live if c.get("issued_by") == "manager"}
        except (Unavailable, ApiError):
            pass
        return ctx["own"]

    # ---------------- 2・3・5 事象の判断 ----------------
    def _step_inbox(self, ctx: dict, st: dict) -> None:
        pend = [e for e in self.store.pending(self.cfg.batch_max * 4)
                if e["kind"] not in ("control.expired_pending",) and not (e["kind"] == "inspection.phase")]
        pend = pend[: self.cfg.batch_max]
        parked_due = self._parked_due()
        st["batch"] = pend
        if not pend and not parked_due:
            return
        sens = {e["id"]: summary.sensitivity_of(e) for e in pend}
        sensitive = [e for e in pend if sens[e["id"]] == "sensitive"]
        normal = [e for e in pend if sens[e["id"]] != "sensitive"]
        sensitive_due, normal_due = [], []
        for todo in parked_due:
            source_id = (todo.get("source") or {}).get("event_id")
            source_event = self.store.event(source_id) if source_id else None
            (sensitive_due if source_event and summary.sensitivity_of(source_event) == "sensitive" else normal_due).append(todo)
        if sensitive or sensitive_due:
            self._handle_sensitive(sensitive, ctx, st, sensitive_due)
        if normal or normal_due:
            self._decide_batch(normal, normal_due, ctx, st, sensitivity="private")

    def _handle_sensitive(self, events: list[dict], ctx: dict, st: dict, parked_due: list[dict] | None = None) -> None:
        """区分が sensitive の事象は、sensitive を受けてよいモデルがあるときだけ別の判断で渡す。無ければ渡さず台帳で parked にする。
        予備役（Claude）へは回さない（D-9.1）。"""
        from axcommon import policy as axpolicy

        models = []
        try:
            models = (self._models_for_role() if self._models_for_role else self.c.resources.models()) or []
        except Exception:  # noqa: BLE001  引けなければ「該当するモデルがない」と同じ扱い（閉じる側）
            models = []
        eligible = [m for m in models if m.get("status", "active") == "active" and m.get("tier") != "last_resort"
                    and axpolicy.allowed("sensitive", m)]
        if eligible:
            self._decide_batch(events, parked_due or [], ctx, st, sensitivity="sensitive")
            return
        sent = []
        for e in events:
            res = self.exec.eject_event(e, "no_sensitive_model", check_at=timeutil.format_ts(self.now() + timedelta(hours=1)))
            if res.get("ok"):
                sent.append(e["id"])
        self.store.mark(sent, "parked", reason="no_sensitive_model", cycle_id=st["cycle_id"])
        st["ejected"] += sent
        st["handled"] += sent

    def _parked_due(self) -> list[dict]:
        """5 check_at を過ぎた parked。見直したものは (id, check_at) で印を付け、同じ件を毎周回は見ない。"""
        try:
            todos = self.c.ledger.todos(state="parked")
        except (Unavailable, ApiError):
            return []
        now = self.now()
        out = []
        for t in todos:
            ca = t.get("check_at")
            if not ca or timeutil.parse_ts(ca) > now or self.store.parked_seen(t["id"], ca):
                continue
            out.append({k: t.get(k) for k in ("id", "title", "reason", "check_at", "project", "subloop", "source")})
        return out[: self.cfg.batch_max]

    def _decide_batch(self, events: list[dict], parked_due: list[dict], ctx: dict, st: dict, *, sensitivity: str) -> None:
        views = [summary.summarize_event(e, str_max=self.cfg.summary_str_max, redact=self.redact) for e in events]
        views.sort(key=lambda v: (v["kind"], v.get("seq") or 0))
        text = prompts.build_inbox_prompt(
            now=timeutil.format_ts(self.now()), events=views, parked_due=self.redact(parked_due),
            plugins=self._plugin_views(ctx), commands=[self._cmd_view(c) for c in ctx.get("live_commands", [])],
            resources=self.redact(ctx.get("resources", [])), lease_min=self.cfg.control_max_lease_min)
        out = self._decide(step="inbox", prompt_text=text, sensitivity=sensitivity, ctx=ctx, st=st)
        ids_ = [e["id"] for e in events]
        if out.status == "ok":
            self._after_ok(out, events, parked_due, ctx, st)
        elif out.status == "unavailable":
            if out.reason == "manager_model_forbidden":
                self._eject_events(events, out.reason, st)
                return
            counts = self.store.bump_attempts(ids_)
            over = [e for e in events if counts.get(e["id"], 0) >= self.cfg.attempts_max]
            self._eject_events(over, f"llm_unavailable:{out.reason}", st)
        else:
            self._record_failure("inbox", out, st, events)
            self._eject_events(events, "bad_output" if out.status == "bad" else "manager_timeout", st)
            for t in parked_due:
                self.store.mark_parked_seen(t["id"], t["check_at"])

    def _plugin_views(self, ctx: dict) -> list[dict]:
        out = []
        for p in ctx.get("plugin_list", []):
            view = {k: p.get(k) for k in ("id", "kind", "mode", "knobs", "revision") if k in p}
            manifest = p.get("manifest")
            if not manifest:
                try:
                    manifest = (self.c.control.plugin(p["id"]) or {}).get("manifest") or {}
                    ctx["plugins"][p["id"]]["manifest"] = manifest
                except (Unavailable, ApiError):
                    manifest = {}
            view["capabilities"] = manifest.get("capabilities", [])
            view["knob_specs"] = manifest.get("knobs", {})
            out.append(self.redact(view))
        return out

    def _after_ok(self, out: Outcome, events: list[dict], parked_due: list[dict], ctx: dict, st: dict) -> None:
        ids_ = [e["id"] for e in events]
        eff = out.effective
        all_unavail = bool(out.results) and all(r.get("unavailable") for r in out.results if r["type"] != "noop") and \
            any(r["type"] != "noop" for r in out.results)
        if all_unavail:
            counts = self.store.bump_attempts(ids_)
            self._eject_events([e for e in events if counts.get(e["id"], 0) >= self.cfg.attempts_max], "api_unavailable", st)
            self._record_ok("inbox", out, st, ids_, [], st["trigger"])
            return
        if not eff and out.rejected and not any(r["type"] == "noop" for r in out.results):
            # 許されない操作しか返らなかった。判断できなかったのと同じ（台帳へ出す）
            self._record_ok("inbox", out, st, ids_, [], st["trigger"])
            self._eject_events(events, "manager_forbidden_action", st)
            for t in parked_due:
                self.store.mark_parked_seen(t["id"], t["check_at"])
            return
        ejected_subjects = {out.actions[r["index"]].get("subject") for r in out.results
                            if r.get("ok") and r["type"] == "ledger.eject"}
        to_eject, handled = [], []
        only_noop = not eff
        for e in events:
            if e["id"] in ejected_subjects:
                handled.append(e["id"])            # LLM が ledger.eject で出した
            elif only_noop and e["kind"] == "error.classified" and (e.get("data") or {}).get("class") == "unknown":
                to_eject.append(e)                 # unknown は様子見では済ませない。判断できなければ台帳へ出す（S-10.2 の 2）
            else:
                handled.append(e["id"])
        self.store.mark(handled, "handled", cycle_id=st["cycle_id"])
        st["handled"] += handled
        if to_eject:
            self._eject_events(to_eject, "unknown_unresolved", st)
        self._record_ok("inbox", out, st, ids_, handled + [e["id"] for e in to_eject if self.store.state_of(e["id"]) == "ejected"], st["trigger"])
        for t in parked_due:
            self.store.mark_parked_seen(t["id"], t["check_at"])

    def _eject_events(self, events: list[dict], reason: str, st: dict) -> None:
        ids_ = []
        for e in events:
            res = self.exec.eject_event(e, reason)
            if res.get("ok"):
                ids_.append(e["id"])
        self.store.mark(ids_, "ejected", reason=reason, cycle_id=st["cycle_id"])
        st["ejected"] += ids_
        st["handled"] += ids_    # 扱った印（オブザーバーの「unknown が扱われない」から外れる。台帳に出したため）

    def _eject_ids(self, ids_: list[str], reason: str, st: dict) -> None:
        by_id = {e["id"]: e for e in self.store.pending(1000)}
        self._eject_events([by_id[i] for i in ids_ if i in by_id], reason, st)

    # ---------------- 4 恒久修繕の ToDo ----------------
    def _step_repairs(self, ctx: dict, st: dict) -> None:
        batch = [e for e in st.get("batch", []) if e["kind"] == "resource.repeat_failure"]
        if not batch:
            return
        try:
            open_titles = {t.get("title") for t in self.c.ledger.todos() if t.get("state") in OPEN_TODO_STATES}
        except (Unavailable, ApiError):
            open_titles = set()
        for e in batch:
            d = e.get("data") or {}
            title = repair_title(d.get("resource_id"), d.get("code"))
            if title in open_titles:
                continue
            res = self.exec.ledger_create({"title": title, "state": "todo", "owner": "system", "project": e.get("project"),
                                           "reason": f"同じ失敗が {d.get('count')} 回続いた（resource.repeat_failure）",
                                           "source": {"event_id": e["id"]}})
            if res.get("ok"):
                open_titles.add(title)
                st.setdefault("repairs", []).append(title)

    # ---------------- 点検の見直し（段 4〜7）----------------
    def _step_review(self, ctx: dict, st: dict) -> None:
        reqs = [e for e in self.store.pending() if e["kind"] == "inspection.phase"]
        if not reqs:
            return
        from .review import Reviewer

        rv = Reviewer(self)
        for e in reqs:
            rv.run(e, ctx, st)


def repair_title(resource_id: Any, code: Any) -> str:
    return f"恒久修繕: {resource_id} {code}"
