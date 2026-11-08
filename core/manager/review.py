"""点検の見直し（S-13.2 の段 4〜7。D-12）。点検係の inspection.phase=review_request を受けて管理役が行う。

4 見直し   系統別の使用量の推移（環境ジョブの利用量 env.job_usage を含む。S-25.24）と、GPT・Claude を選んだ理由の内訳を集計し
           （機械的。LLM は使わない）、HQ が読む manager.decision(step=review) の report に載せる。LLM には集計を渡し、提案を ToDo か
           提案カードで返してもらう。設定変更は利用者が行う（提案は記録だけ）。
5 退役     excluded が続いたモデルの retired への変更を提案する（report.retire_candidates。提案カードは LLM の返答か空）。
6 恒久修繕 繰り返した失敗（resource.repeat_failure）と、予備役の件（backup.activated）の修繕を ToDo にする。予備役の件が最優先
           （先に作り、題の頭に【予備役の件】を付ける。ToDo に優先度の項目が無いため。DECISIONS）。
7 要約     確定した知識を記憶コネクタへ送る（S-14.2）。
最後に manager.decision(step=review) を 1 件書く。点検係はこれを見て段 8（再開）へ進む。
集計に使う事象は、前回の見直しの seq 以後（時系列記録に時刻の絞り込みが無いため。DECISIONS）。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from axcommon import timeutil
from axcommon.client import ApiError, Unavailable

from . import COMPONENT, prompt as prompts

REPORT_KINDS = ("worker.selection", "worker.usage", "env.job_usage", "resource.state", "resource.repeat_failure", "backup.activated")
MAX_PAGES = 50
PAGE = 200


def family_of(model_id: str | None) -> str:
    if not model_id:
        return "unknown"
    head = str(model_id).split("/", 1)[0].lower()
    return {"codex": "gpt", "claude": "claude"}.get(head, head)


def aggregate(events: list[dict], models: list[dict], *, truncated: bool = False) -> dict:
    """集計（純粋な関数）。事象の data の形は b6a の実装に合わせて緩く読む（無い項目は数えない）。"""
    fam_jobs: dict[str, int] = defaultdict(int)
    fam_tokens: dict[str, dict[str, int]] = defaultdict(lambda: {"input": 0, "output": 0})
    trend: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    reasons: dict[str, dict[str, int]] = {"gpt": defaultdict(int), "claude": defaultdict(int)}
    envs: dict[str, dict[str, float]] = defaultdict(lambda: {"jobs": 0, "run_s": 0.0})
    repeat: dict[tuple, int] = {}
    backup = 0
    for ev in events:
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        day = str(ev.get("ts", ""))[:10]
        kind = ev.get("kind")
        if kind == "worker.selection":
            sel = data.get("selected") or data.get("selected_id") or (data.get("selection") or {}).get("selected") or data.get("model")
            fam = family_of(sel)
            fam_jobs[fam] += 1
            trend[fam][day] += 1
            if fam in reasons:
                reasons[fam][str(data.get("reason") or (data.get("selection") or {}).get("reason") or "unknown")] += 1
        elif kind == "worker.usage":
            u = data.get("usage") or {}
            fam = family_of(data.get("model"))
            fam_tokens[fam]["input"] += int(u.get("input_tokens") or 0)
            fam_tokens[fam]["output"] += int(u.get("output_tokens") or 0)
        elif kind == "env.job_usage":
            e = envs[str(data.get("env_id") or "unknown")]
            e["jobs"] += 1
            e["run_s"] += float(data.get("run_s") or 0)
        elif kind == "resource.repeat_failure":
            repeat[(data.get("resource_id"), data.get("code"))] = int(data.get("count") or 0)
        elif kind == "backup.activated":
            backup += 1
    retire = [m.get("id") for m in models if m.get("status") == "excluded"]
    return {
        "truncated": truncated, "events": len(events),
        "usage_by_family": {f: {"jobs": fam_jobs.get(f, 0), "tokens": fam_tokens.get(f, {"input": 0, "output": 0})}
                            for f in sorted(set(fam_jobs) | set(fam_tokens))},
        "trend": {f: dict(sorted(d.items())) for f, d in sorted(trend.items())},
        "selection_reasons": {f: dict(sorted(d.items())) for f, d in reasons.items()},
        "env_job_usage": {k: dict(v) for k, v in sorted(envs.items())},
        "retire_candidates": sorted(i for i in retire if i),
        "repeat_failures": [{"resource_id": r, "code": c, "count": n} for (r, c), n in sorted(repeat.items(), key=lambda x: str(x[0]))],
        "backup_activations": backup,
    }


class Reviewer:
    def __init__(self, mgr):
        self.m = mgr

    def _collect(self) -> tuple[list[dict], bool, int]:
        m = self.m
        after = int(m.store.meta("review_cursor", "0") or 0)
        out, truncated = [], False
        for _ in range(MAX_PAGES):
            try:
                r = m.c.timeline.get_events(after=after, kind=",".join(REPORT_KINDS), limit=PAGE) or {}
            except (Unavailable, ApiError):
                break
            evs = r.get("events") or []
            out += evs
            nxt = r.get("next_after")
            if len(evs) < PAGE or not isinstance(nxt, int) or nxt <= after:
                after = nxt if isinstance(nxt, int) and nxt > after else after
                break
            after = nxt
        else:
            truncated = True
        return out, truncated, after

    def run(self, ev: dict, ctx: dict, st: dict) -> None:
        m = self.m
        plan = ((ev.get("data") or {}).get("plan") or {})
        inspection_id = plan.get("inspection_id") or ev["id"]
        events, truncated, after = self._collect()
        try:
            models = m.c.resources.models() if m.c.resources else []
        except (Unavailable, ApiError):
            models = []
        report = aggregate(events, models, truncated=truncated)
        # Surface already-created priority repairs before asking for other proposals.
        try:
            open_todos = m.c.ledger.todos()
            report["priority_repairs"] = [
                {"id": t.get("id"), "title": t.get("title"), "reason": t.get("reason")}
                for t in open_todos
                if t.get("reason") == "backup:root_cause:top"
                and t.get("state") in ("todo", "running", "review", "parked", "waiting_user")
            ]
        except (Unavailable, ApiError):
            report["priority_repairs"] = None
        steps: dict[str, Any] = {"4": "report", "5": "report", "6": "skipped", "7": "skipped"}
        # 4・5 LLM に提案を頼む（失敗しても 6・7 は進める）
        out = m._decide(step="review", prompt_text=prompts.build_review_prompt(now=timeutil.now_ts(), inspection_id=inspection_id, report=report,
                                                                                lease_min=m.cfg.control_max_lease_min),
                        sensitivity="private", ctx=ctx, st=st, allow=("ledger.add", "ledger.eject", "noop"))
        if out.status == "ok":
            steps["4"] = steps["5"] = "ok"
            st["decisions"] += 1
            st["executed"] += len(out.effective)
            st["rejected"] += len(out.rejected)
        else:
            steps["4"] = steps["5"] = f"llm_{out.status}"
            if out.status in ("bad", "timeout"):
                m._record_failure("review", out, st, [ev])
        # 6 恒久修繕（予備役の件が最優先）
        repairs = self._repairs(events, st)
        steps["6"] = "ok"
        # 7 要約
        mem = self._remember(ev, inspection_id, report, repairs)
        steps["7"] = mem
        m.store.set_meta("review_cursor", str(after))
        m.store.mark([ev["id"]], "handled", cycle_id=st["cycle_id"])
        st["handled"].append(ev["id"])
        m.sink.emit("manager.decision", COMPONENT, {
            "cycle_id": st["cycle_id"], "step": "review", "inspection_id": inspection_id, "trigger": "inspection.review_request",
            "model": out.model, "job_id": out.job_id, "event_ids": [ev["id"]], "handled": [ev["id"]],
            "report": report, "steps": steps, "repairs": len(repairs), "backup_activations": report["backup_activations"],
            "actions": [{"type": a.get("type")} for a in out.actions], "executed": out.results, "rejected": out.rejected},
            cause=ev["id"] if len(ev["id"]) == 26 else None)

    def _repairs(self, events: list[dict], st: dict) -> list[str]:
        from .cycle import OPEN_TODO_STATES, repair_title

        m = self.m
        try:
            open_todos = [t for t in m.c.ledger.todos() if t.get("state") in OPEN_TODO_STATES]
            open_titles = {t.get("title") for t in open_todos}
            root_events = {(t.get("source") or {}).get("event_id") for t in open_todos
                           if t.get("reason") in ("backup:root_cause", "backup:root_cause:top")}
        except (Unavailable, ApiError):
            open_titles = set()
            root_events = set()
        made: list[str] = []
        todos = []
        for e in events:
            if e["kind"] == "backup.activated" and e.get("id") not in root_events:
                reason = str((e.get("data") or {}).get("reason", ""))[:120]
                todos.append((0, f"【予備役の件】根本原因の修繕: {reason}", e, "予備役が起動した（backup.activated）。最優先"))
        for e in events:
            if e["kind"] == "resource.repeat_failure":
                d = e.get("data") or {}
                todos.append((1, repair_title(d.get("resource_id"), d.get("code")), e, f"同じ失敗が {d.get('count')} 回続いた"))
        for _prio, title, e, reason in sorted(todos, key=lambda x: x[0]):
            if title in open_titles:
                continue
            res = m.exec.ledger_create({"title": title, "state": "todo", "owner": "system", "reason": reason, "source": {"event_id": e["id"]}})
            if res.get("ok"):
                open_titles.add(title)
                made.append(title)
        st.setdefault("repairs", []).extend(made)
        return made

    def _remember(self, ev: dict, inspection_id: str, report: dict, repairs: list[str]) -> str:
        m = self.m
        if m.c.memory is None:
            return "skipped"
        fam = report["usage_by_family"]
        content = (f"点検 {inspection_id} の見直し。系統別のジョブ数 " + ", ".join(f"{f}={v['jobs']}" for f, v in fam.items())
                   + f"。恒久修繕の ToDo {len(repairs)} 件。予備役の起動 {report['backup_activations']} 回。除外が続くモデル "
                   + (", ".join(report["retire_candidates"]) or "なし") + "。")
        body = {"project": "ax", "subloop": "core", "host": m.cfg.host, "source": ev["id"], "kind": "result", "content": content[:2000],
                "refs": [f"event:{ev['id']}"]}
        return m.exec.remember(body)
