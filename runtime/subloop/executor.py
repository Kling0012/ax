"""グラフの実行系（D-6.2、S-7.5）。

- scheduler: 予定の時刻に run を作る。間に合わない予定は `missed` を書いて追いかけない。mode と資源の都合で起動できなければ `skipped`。
- executor: entry から辺をたどる。item_source の出力で項目に分け、max_parallel_items 個ずつ並行に進める（asyncio の Task）。
  各ノード: mode を確かめ → node.state を書き → 型を呼ぶ → 出力の status で辺を選ぶ → 上限（retries、continuations、max_loops、max_hops）を数える。
- 1 つの項目の失敗（上限、例外、依存先の ejected）は、その項目だけを外へ出す。他の項目と次の実行は進む（必須）。
- 二重実行の防止: 外へ影響する型は ctx.launching() を外部操作の直前に呼ぶ。recover() が起動時に終わっていない launching を型ごとの reconcile で照合する。
  照合できなければ `lost`（再送しない）。
- mode: ノードを起動する直前に最新の mode を見る（draining は新しい run・項目を始めない。既存項目の後続は最後まで。collect_only は
  needs_mode に collect_only を含むノードだけ起動し、項目は planned のまま止める）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from axcommon import ids, timeutil

from . import schedule, template
from .ctx import NodeContext, Services
from .graph import Graph, Node
from .graphcheck import _with_errors
from .nodeapi import EjectRequest
from .nodes import Registry
from .store import Store

log = logging.getLogger("ax.executor")

NODE_STATUSES = ("ok", "bug", "error", "skip")


@dataclass
class Result:
    status: str
    out: dict
    eject: dict | None = None
    nr_id: int | None = None


@dataclass
class Outcome:
    kind: str                      # completed | ejected | held | stopped
    result: Result | None = None
    reason: str | None = None


def _iso(dt) -> str:
    return timeutil.format_ts(dt)


class Executor:
    def __init__(self, *, svc: Services, graph: Graph, manifest: dict, registry: Registry, store: Store):
        self.svc = svc
        self.graph = graph
        self.manifest = manifest
        self.registry = registry
        self.store = store
        # Sending subloops must hear from control before resuming a saved run mode.
        from core.control import SENDING_OUTPUTS

        sends_externally = bool(set(manifest.get("outputs") or []) & SENDING_OUTPUTS)
        self.mode = "collect_only" if sends_externally else "run"
        self.revision = 0
        self._knob_over: dict[str, Any] = dict(store.get_meta("knob_over", {}) or {})
        self._mode_saved = store.get_meta("mode")
        if self._mode_saved and not (sends_externally and self._mode_saved == "run"):
            self.mode = self._mode_saved
        self.revision = int(store.get_meta("revision", 0) or 0)
        self._tasks: dict[str, asyncio.Task] = {}
        self._patrol_active = False
        self._run_now = False

    # ------------------------------------------------------------------ 設定
    def knobs(self) -> dict[str, Any]:
        out = {k: v.get("default") for k, v in (self.manifest.get("knobs") or {}).items() if isinstance(v, dict)}
        out.update(self._knob_over)
        return out

    def apply(self, mode: str | None, knobs: dict | None, revision: int | None, *, run_now: bool = False) -> bool:
        """POST /apply と effective の読みから呼ぶ。revision が手元より古ければ無視する（S-4.4）。"""
        if revision is not None and revision < self.revision:
            return False
        if mode is not None:
            self.mode = mode
            self.store.set_meta("mode", mode)
        if knobs is not None:
            self._knob_over = dict(knobs)
            self.store.set_meta("knob_over", self._knob_over)
        if revision is not None:
            self.revision = revision
            self.store.set_meta("revision", revision)
        if run_now:
            self._run_now = True
        return True

    def max_parallel(self) -> int:
        try:
            v = template.render(self.graph.max_parallel_items, {"knobs": self.knobs()})
            return max(1, int(v))
        except Exception:  # noqa: BLE001
            return 1

    def _int_default(self, name: str) -> int:
        return int(self.knobs().get(name, self.svc.defaults.get(name)))

    # ------------------------------------------------------------------ 事象
    def _run_state(self, run_id: str, state: str, reason: str | None = None) -> None:
        self.store.set_run_state(run_id, state, reason)
        self.svc.emit("run.state", run_id, {"state": state, "reason": reason}, run_id=run_id)

    def _item_state(self, item: dict, state: str, reason: str | None = None, artifacts: dict | None = None) -> None:
        self.store.set_item_state(item["item_id"], state, reason)
        data: dict[str, Any] = {"item_id": item["item_id"], "state": state, "reason": reason}
        if item.get("todo_id"):
            data["todo_id"] = item["todo_id"]
        if artifacts:
            data["artifacts"] = artifacts
        self.svc.emit("item.state", item["item_id"], data, run_id=item.get("run_id"))

    # ------------------------------------------------------------------ scheduler
    def _schedule(self) -> dict:
        return self.manifest.get("schedule") or {}

    def _every_s(self) -> int | None:
        s = self._schedule()
        if s.get("kind") == "interval":
            v = self.knobs().get("every_s", s.get("every_s"))
            return int(v) if v else None
        return None

    async def tick(self) -> None:
        """scheduler と項目の起動を 1 回進める。本番は数秒ごとに呼ぶ。"""
        try:
            await self._scheduler_step()
        except Exception as e:  # noqa: BLE001
            log.error("scheduler の失敗: %s", type(e).__name__)
        await self._pump()

    def _record_missed(self, planned_at: str, reason: str) -> None:
        rid = ids.new_ulid()
        self.store.create_run(rid, state="missed", planned_at=planned_at, trigger="schedule", reason=reason)
        self.store.set_run_state(rid, "missed", reason)
        self.svc.emit("run.state", rid, {"state": "missed", "reason": reason, "planned_at": planned_at}, run_id=rid)

    def free_slots(self) -> int:
        """inbox.take が取ってよい個数: max_parallel_items から、実行中と待っている項目の数を引く。"""
        live = [i for i in self.store.items(["running", "planned"]) if not i["item_id"].startswith("run:")]
        return max(0, self.max_parallel() - len(live))

    def _inbox_ready(self) -> bool:
        return bool(self.manifest.get("accepts_items")) and self.mode == "run" and self.store.inbox_queued() > 0 and self.free_slots() > 0

    def kick(self) -> None:
        """項目を受け取った直後に呼ぶ。mode が run なら、受け取りの直後に run を作る（S-7.10）。それ以外は保存だけ。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._inbox_ready() and not self._patrol_active:
            loop.create_task(self.run_once(trigger="items"))

    async def _scheduler_step(self) -> None:
        if self._run_now:
            self._run_now = False
            await self.run_once(trigger="run_now")
        if self._inbox_ready() and not self._patrol_active:
            await self.run_once(trigger="inbox")
        sched = self._schedule()
        kind = sched.get("kind")
        if kind not in ("interval", "cron"):
            return
        spec = dict(sched)
        if kind == "interval":
            every = self._every_s()
            if not every:
                return
            spec["every_s"] = every
        dec = schedule.decide(kind, spec, self.store.get_meta("next_due"), timeutil.now())
        self.store.set_meta("next_due", dec.next_due)
        for ts in dec.missed:
            self._record_missed(ts, "late")
        if dec.run:
            await self.run_once(trigger="schedule", planned_at=dec.run)

    # ------------------------------------------------------------------ run
    def _pipeline_start(self) -> str | None:
        src = self.graph.item_source
        if not src:
            return self.graph.entry
        e = self.graph.edge_for(src, "ok")
        return e.dst if e else None

    async def run_once(self, trigger: str = "schedule", planned_at: str | None = None) -> str | None:
        """1 回の run（巡回）を作って走らせる。戻りは run_id（作らなかったら None）。"""
        if self.mode in ("poll_paused", "draining"):
            rid = ids.new_ulid()
            self.store.create_run(rid, state="skipped", planned_at=planned_at, trigger=trigger, reason=f"mode:{self.mode}")
            self.store.set_run_state(rid, "skipped", f"mode:{self.mode}")
            self.svc.emit("run.state", rid, {"state": "skipped", "reason": f"mode:{self.mode}"}, run_id=rid)
            return None
        if self._patrol_active:
            self._record_missed(planned_at or timeutil.now_ts(), "previous_run_active")
            return None
        rid = ids.new_ulid()
        self.store.create_run(rid, state="planned", planned_at=planned_at, trigger=trigger)
        self.svc.emit("run.state", rid, {"state": "planned", "reason": None}, run_id=rid)
        self._patrol_active = True
        try:
            if self.graph.item_source:
                await self._patrol(rid)
            else:
                await self._single_item_run(rid)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  run の失敗は他の run に影響させない
            log.error("run %s の失敗: %s", rid, type(e).__name__)
            self._run_state(rid, "failed", f"internal:{type(e).__name__}")
        finally:
            self._patrol_active = False
        return rid

    async def _single_item_run(self, rid: str) -> None:
        """item_source が無い: 実行全体が 1 項目（item_id = run_id）。"""
        self.store.add_item(rid, rid, {}, state="planned")
        self._run_state(rid, "running", None)
        await self._pump()

    async def _patrol(self, rid: str) -> None:
        pseudo_id = f"run:{rid}"
        self.store.add_item(pseudo_id, rid, {}, state="running")
        self.store.update_item(pseudo_id, started=False)
        self._run_state(rid, "launching", None)
        self._run_state(rid, "running", None)
        item = self.store.get_item(pseudo_id)
        out = await self._walk(item, self.graph.entry, patrol=True, stop_at=self.graph.item_source)
        if out.kind == "stopped" and out.result is not None:
            self.store.set_item_state(pseudo_id, "completed", None)
            items = (out.result.out.get("data") or {}).get("items") or []
            self._register_items(rid, items)
            self._maybe_complete_run(rid)
            await self._pump()
        elif out.kind == "held":
            self.store.set_item_state(pseudo_id, "completed", "held")
            self._run_state(rid, "skipped", f"mode:{self.mode}")
        elif out.kind == "completed":
            self.store.set_item_state(pseudo_id, "completed", None)
            self._maybe_complete_run(rid)
        else:
            self.store.set_item_state(pseudo_id, "ejected", out.reason)
            self._run_state(rid, "failed", out.reason)

    def _register_items(self, rid: str, items: list) -> None:
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                it = {"value": it}
            base = str(it.get("item_id") or it.get("id") or it.get("item_key") or it.get("key") or f"{rid}-{i}")
            iid = base
            ex = self.store.get_item(iid)
            if ex is not None:
                if ex["state"] == "ejected":
                    iid = f"{base}~{rid[-6:].lower()}"  # 外へ出した項目の修正は新しい項目として拾う（S-7.5）
                    if self.store.get_item(iid) is not None:
                        continue
                else:
                    continue
            data = dict(it)
            data["item_id"] = iid
            ok = self.store.add_item(iid, rid, data, todo_id=it.get("todo_id"), depends_on=list(it.get("depends_on") or []), state="planned")
            if ok:
                self.svc.emit("item.state", iid, {"item_id": iid, "state": "planned", "reason": None, **({"todo_id": it["todo_id"]} if it.get("todo_id") else {})}, run_id=rid)

    def _maybe_complete_run(self, rid: str) -> None:
        run = self.store.get_run(rid)
        if run is None or run["state"] not in ("running", "launching", "planned"):
            return
        live = [i for i in self.store.items(["planned", "running"], run_id=rid) if not i["item_id"].startswith("run:")]
        if live:
            return
        self._run_state(rid, "completed", None)

    # ------------------------------------------------------------------ 項目
    def _mode_allows_start(self, node: Node) -> bool:
        if self.mode in ("run", "poll_paused"):
            return True
        if self.mode == "collect_only":
            return "collect_only" in node.needs_mode
        return False

    def _gate(self, item: dict, node: Node, patrol: bool) -> bool:
        if patrol:
            if self.mode == "run":
                return True
            if self.mode == "collect_only":
                return "collect_only" in node.needs_mode
            return False
        if self.mode == "collect_only":
            return "collect_only" in node.needs_mode
        fresh = self.store.get_item(item["item_id"]) or item
        if fresh.get("started"):
            return True  # 実行中の物は続ける（draining でも後続は最後まで）
        return self._mode_allows_start(node)

    async def _pump(self) -> None:
        """planned の項目を、枠と mode と依存の許す限り起動する。"""
        self._tasks = {k: t for k, t in self._tasks.items() if not t.done()}
        limit = self.max_parallel()
        start = self._pipeline_start()
        wait_h = self._int_default("dependency_wait_h")
        for it in self.store.items(["planned"]):
            if it["item_id"].startswith("run:"):
                continue
            if it["item_id"] in self._tasks:
                continue
            # 依存
            blocked = False
            for dep in it["depends_on"]:
                d = self.store.get_item(dep)
                if d is not None and d["state"] == "ejected":
                    await self._eject(it, "dependency_ejected", todo_spec=None)
                    blocked = True
                    break
                if d is None or d["state"] != "completed":
                    created = timeutil.parse_ts(it["created_at"])
                    if timeutil.now() - created > timedelta(hours=wait_h):
                        await self._eject(it, "dependency_timeout", todo_spec=None)
                    blocked = True
                    break
            if blocked:
                continue
            if len(self._tasks) >= limit:
                continue
            if start is None:
                self._item_state(it, "completed", None)
                self._maybe_complete_run(it["run_id"])
                continue
            if not self._mode_allows_start(self.graph.nodes[start]):
                continue
            self.store.set_item_state(it["item_id"], "running", None)
            self._tasks[it["item_id"]] = asyncio.create_task(self._run_item(it["item_id"], start))

    async def _run_item(self, item_id: str, start: str, pending: Result | None = None) -> None:
        item = self.store.get_item(item_id)
        if item is None:
            return
        try:
            out = await self._walk(item, start, patrol=False, pending=pending)
            if out.kind == "held":
                # 始める前に mode が変わった。planned に戻して待つ
                self.store.set_item_state(item_id, "planned", None)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  1 項目の失敗を他へ広げない（必須）
            log.error("項目 %s の失敗: %s", item_id, type(e).__name__)
            fresh = self.store.get_item(item_id)
            if fresh and fresh["state"] not in ("ejected", "completed"):
                await self._eject(fresh, f"internal:{type(e).__name__}", todo_spec=None)
        finally:
            fresh = self.store.get_item(item_id)
            if fresh and fresh.get("run_id"):
                self._maybe_complete_run(fresh["run_id"])

    async def drain(self, *, rounds: int = 50) -> None:
        """試験と停止処理用: 動いている項目が終わるまで待つ。"""
        for _ in range(rounds):
            tasks = [t for t in self._tasks.values() if not t.done()]
            if not tasks:
                await self._pump()
                tasks = [t for t in self._tasks.values() if not t.done()]
                if not tasks:
                    return
            await asyncio.gather(*tasks, return_exceptions=True)

    def crash(self) -> None:
        """試験用: 項目の Task を、後始末なしで取り消す（プロセスが落ちた状態を作る）。"""
        for t in self._tasks.values():
            t.cancel()

    # ------------------------------------------------------------------ 歩く
    def _select_next(self, node: Node, status: str, out: dict | None = None) -> tuple[str, str | None, Any]:
        """('node', ノード ID, 辺) | ('done', None, None) | ('eject', 理由, None)。ok で data.outcome があり、その結果名の辺があればそれを優先する。"""
        g = self.graph
        if status == "ok":
            oc = ((out or {}).get("data") or {}).get("outcome")
            if isinstance(oc, str):
                e = g.edge_for(node.id, f"outcome:{oc}")
                if e is not None:
                    return ("node", e.dst, e)
        if status in ("ok", "skip"):
            e = g.edge_for(node.id, status)
            if e is None and status == "skip":
                return ("done", None, None)
            if e is None:
                return ("done", None, None)
            return ("node", e.dst, e)
        if status == "bug":
            e = g.edge_for(node.id, "bug")
            if e is not None:
                return ("node", e.dst, e)
        return self._on_fail(node, status)

    def _on_fail(self, node: Node, status: str) -> tuple[str, str | None, Any]:
        of = node.on_fail or "eject"
        if of == "skip":
            e = self.graph.edge_for(node.id, "skip")
            return ("node", e.dst, e) if e else ("done", None, None)
        if of.startswith("goto:"):
            return ("node", of[5:], None)
        return ("eject", f"{node.id}:{status}", None)

    async def _enter(self, item: dict, dst: str, edge: Any, src: str) -> str | None:
        """次のノードへ入る前に上限を数える。超えたら理由を返す。"""
        iid = item["item_id"]
        if not self.store.visited(iid, dst):
            return None
        fresh = self.store.get_item(iid) or item
        hops = int(fresh.get("hops", 0)) + 1
        self.store.update_item(iid, hops=hops)
        key = edge.key if edge is not None else f"goto:{src}->{dst}"
        n = self.store.bump_edge(iid, key)
        if edge is not None and isinstance(edge.max_loops, int) and n > edge.max_loops:
            return f"max_loops:{edge.src}->{edge.dst}"
        limit = self._int_default("max_hops")
        if hops > limit:
            return f"max_hops:{limit}"
        return None

    async def _walk(self, item: dict, start: str, *, patrol: bool, stop_at: str | None = None, pending: Result | None = None) -> Outcome:
        cur = start
        res_override = pending
        while True:
            node = self.graph.nodes[cur]
            self.store.update_item(item["item_id"], current_node=cur)
            if res_override is None:
                if not self._gate(item, node, patrol):
                    return Outcome("held", reason=f"mode:{self.mode}")
                if not patrol:
                    self.store.update_item(item["item_id"], started=True)
                res = await self._exec_node(item, node)
            else:
                res, res_override = res_override, None
            fresh = self._merge_artifacts(item, res.out) if not patrol else (self.store.get_item(item["item_id"]) or item)
            if res.eject is not None:
                await self._eject(fresh, res.eject["reason"], todo_spec=res.eject.get("todo"), artifacts=res.eject.get("artifacts"))
                return Outcome("ejected", reason=res.eject["reason"])
            if stop_at is not None and node.id == stop_at and res.status == "ok":
                return Outcome("stopped", result=res)
            kind, target, edge = self._select_next(node, res.status, res.out)
            if kind == "done":
                fresh = self.store.get_item(item["item_id"]) or item
                if not patrol:
                    self._item_state(fresh, "completed", None, artifacts=fresh.get("artifacts") or None)
                return Outcome("completed")
            if kind == "eject":
                code = (res.out.get("error") or {}).get("code") or res.status
                reason = f"{node.id}:{code}"
                if patrol:
                    return Outcome("ejected", reason=reason)
                await self._eject(fresh, reason, todo_spec=None)
                return Outcome("ejected", reason=reason)
            assert target is not None
            over = await self._enter(item, target, edge, node.id)
            if over is not None:
                if patrol:
                    return Outcome("ejected", reason=over)
                await self._eject(self.store.get_item(item["item_id"]) or item, over, todo_spec=None)
                return Outcome("ejected", reason=over)
            cur = target

    def _merge_artifacts(self, item: dict, out: dict) -> dict:
        fresh = self.store.get_item(item["item_id"]) or item
        if out.get("artifacts"):
            arts = dict(fresh.get("artifacts") or {})
            arts.update(out["artifacts"])
            self.store.update_item(item["item_id"], artifacts=arts)
            fresh = self.store.get_item(item["item_id"]) or fresh
        return fresh

    # ------------------------------------------------------------------ ノードを 1 つ
    def _scope(self, item: dict) -> dict[str, Any]:
        data = dict(item.get("data") or {})
        data.setdefault("item_id", item["item_id"])
        return {"knobs": self.knobs(), "item": data, "nodes": self.store.node_outputs(item["item_id"])}

    async def _exec_node(self, item: dict, node: Node) -> Result:
        t = self.registry.get(node.type)
        iid = item["item_id"]
        run_id = item.get("run_id") or ""
        retries = int(node.limits.get("retries", self.svc.defaults.get("system_retries")))
        max_cont = int(node.limits.get("continuations", 0))
        timeout_s = int(node.limits.get("timeout_s", 60))
        tries = 0
        cont = 0
        while True:
            attempt = self.store.next_attempt(iid, node.id)
            nr_id = self.store.start_node_run(run_id, iid, node.id, attempt, cont)
            self.svc.emit("node.state", f"{iid}/{node.id}", {"node": node.id, "state": "running", "attempt": attempt}, run_id=run_id, node=node.id)
            deadline = _iso(timeutil.now() + timedelta(seconds=timeout_s))
            out: dict
            timed_out = False
            try:
                if t is None:
                    out = _err("type_unknown", f"型 {node.type} が部品庫に無い")
                else:
                    with_ = template.render(node.with_, self._scope(item))
                    errs = _with_errors(t.schema, with_)
                    if errs:
                        out = _err("with_invalid", errs[0])
                    else:
                        fresh = self.store.get_item(iid) or item
                        ctx = NodeContext(self, item=self._scope(fresh)["item"], node=node, run_id=run_id, nr_id=nr_id, attempt=attempt,
                                          continuation=cont, deadline=deadline)
                        inp = {"run_id": run_id, "item": ctx.item, "with": with_, "artifacts": {"workdir": "", "files": []}, "deadline": deadline}
                        out = await asyncio.wait_for(t.run(ctx, inp), timeout=timeout_s)
            except asyncio.TimeoutError:
                timed_out = True
                out = _err("node_timeout", f"{timeout_s} 秒を超えた", expected=False)
            except EjectRequest as e:
                out = {"status": "error", "data": {}, "error": {"class": "unknown", "code": e.reason, "message": str(e)[:300]},
                       "_eject": {"reason": e.reason, "todo": e.todo, "artifacts": e.artifacts}}
            except asyncio.CancelledError:
                raise
            except template.TemplateError as e:
                out = _err("with_invalid", str(e))
            except Exception as e:  # noqa: BLE001
                c = self.svc.errors.classify_exception(e) if self.svc.errors is not None else None
                if c is not None and c.cls == "system.expected":
                    out = _err(c.code, f"{type(e).__name__}", expected=True)
                else:
                    out = _err("node_exception", type(e).__name__)
            out = self._normalize(out)
            status = out["status"]
            ejectreq = out.pop("_eject", None)
            self.store.finish_node_run(nr_id, "timed_out" if timed_out else "done", status, out, out.get("error"))
            self.svc.emit("node.state", f"{iid}/{node.id}", {"node": node.id, "state": "timed_out" if timed_out else status, "attempt": attempt},
                          run_id=run_id, node=node.id)
            if status == "error" and out.get("error"):
                self.svc.emit("error.classified", f"{iid}/{node.id}", {"class": out["error"]["class"], "code": out["error"]["code"],
                                                                         "message": (out["error"].get("message") or "")[:300]}, run_id=run_id, node=node.id)
            if ejectreq is not None:
                return Result(status, out, eject=ejectreq, nr_id=nr_id)
            if timed_out and cont < max_cont:
                cont += 1
                continue
            if status == "error" and (out.get("error") or {}).get("class") == "system.expected" and tries < retries:
                await self.svc.sleep(self.svc.defaults.retry_wait_s(tries))
                tries += 1
                continue
            return Result(status, out, nr_id=nr_id)

    @staticmethod
    def _normalize(out: Any) -> dict:
        if not isinstance(out, dict) or out.get("status") not in NODE_STATUSES:
            return _err("bad_output", "型の出力が S-7.3 の形でない")
        o = {k: out[k] for k in ("status", "data", "artifacts", "error", "_eject") if k in out}
        o.setdefault("data", {})
        if o["status"] == "error" and not o.get("error"):
            o["error"] = {"class": "unknown", "code": "error", "message": ""}
        return o

    # ------------------------------------------------------------------ 外へ出す
    async def _eject(self, item: dict, reason: str, *, todo_spec: dict | None, artifacts: dict | None = None) -> None:
        """項目を外へ出す（S-7.5、D-6.2）。todo_id を持つ項目は ToDo を作らず item.state(ejected, todo_id) を書く。台帳が元の ToDo を遷移させる。"""
        fresh = self.store.get_item(item["item_id"]) or item
        arts = dict(fresh.get("artifacts") or {})
        arts.update(artifacts or {})
        if arts:
            self.store.update_item(item["item_id"], artifacts=arts)
        todo_id = fresh.get("todo_id")
        if not todo_id:
            try:
                spec = dict(todo_spec or {})
                spec.setdefault("title", f"項目を外へ出した: {fresh['item_id']}"[:200])
                spec.setdefault("state", "parked")
                spec.setdefault("owner", "system")
                spec.setdefault("project", (fresh.get("data") or {}).get("project"))
                spec["subloop"] = self.svc.plugin_id
                if spec["state"] == "parked":
                    spec.setdefault("reason", reason)
                    spec.setdefault("check_at", timeutil.add_seconds(timeutil.now_ts(), 86400))
                spec["source"] = {"run_id": fresh.get("run_id") or "", "item_id": fresh["item_id"]}
                await asyncio.to_thread(self.svc.ledger.create_todo, spec)
            except Exception as e:  # noqa: BLE001  台帳に出せなくても、ejected の事象は残す（オブザーバーが拾う）
                log.error("ToDo を作れない: %s", type(e).__name__)
        self.store.set_item_state(item["item_id"], "ejected", reason)
        data: dict[str, Any] = {"item_id": item["item_id"], "state": "ejected", "reason": reason}
        if todo_id:
            data["todo_id"] = todo_id
        if arts:
            data["artifacts"] = arts
        self.svc.emit("item.state", item["item_id"], data, run_id=fresh.get("run_id"))
        if fresh.get("run_id") and not item["item_id"].startswith("run:"):
            run = self.store.get_run(fresh["run_id"])
            if run and not self.graph.item_source and run["state"] in ("running", "launching", "planned"):
                self._run_state(fresh["run_id"], "ejected", reason)
            else:
                self._maybe_complete_run(fresh["run_id"])

    # ------------------------------------------------------------------ 再起動の後始末（二重実行の防止）
    async def recover(self) -> dict[str, list[str]]:
        """起動時に 1 回。終わっていない launching を型ごとの reconcile で照合し、落ちた時に動いていた項目を続ける。
        戻り: {"resolved": [...], "retry": [...], "lost": [...], "resumed": [...]}（node_run の ID 文字列）。"""
        rep: dict[str, list[str]] = {"resolved": [], "retry": [], "lost": [], "resumed": []}
        pending_results: dict[str, tuple[str, Result | None]] = {}
        for nr in self.store.pending_launching():
            item = self.store.get_item(nr["item_id"])
            node = self.graph.nodes.get(nr["node_id"])
            t = self.registry.get(node.type) if node else None
            verdict: dict | None = None
            if t is not None and t.reconcile is not None and node is not None:
                try:
                    ctx = NodeContext(self, item=self._scope(item)["item"] if item else {"item_id": nr["item_id"]}, node=node, run_id=nr["run_id"] or "",
                                      nr_id=nr["id"], attempt=nr["attempt"], continuation=nr["continuation"], deadline=timeutil.now_ts())
                    verdict = await t.reconcile(ctx, nr["launching_key"])
                except Exception as e:  # noqa: BLE001
                    log.error("reconcile の失敗 %s: %s", nr["node_id"], type(e).__name__)
                    verdict = None
            tag = f"{nr['item_id']}/{nr['node_id']}#{nr['attempt']}"
            if verdict is not None and verdict.get("found") is True:
                out = self._normalize(verdict.get("out") or {"status": "ok", "data": {}})
                self.store.finish_node_run(nr["id"], "done", out["status"], out, out.get("error"))
                self.svc.emit("node.state", f"{nr['item_id']}/{nr['node_id']}", {"node": nr["node_id"], "state": "reconciled", "attempt": nr["attempt"]},
                              run_id=nr["run_id"] or None, node=nr["node_id"])
                rep["resolved"].append(tag)
                if item and item["state"] == "running":
                    pending_results[item["item_id"]] = (nr["node_id"], Result(out["status"], out, nr_id=nr["id"]))
            elif verdict is not None and verdict.get("found") is False:
                self.store.finish_node_run(nr["id"], "abandoned", None, None, None)
                rep["retry"].append(tag)
                if item and item["state"] == "running":
                    pending_results[item["item_id"]] = (nr["node_id"], None)
            else:
                self.store.finish_node_run(nr["id"], "lost", None, None, {"class": "unknown", "code": "lost", "message": "照合できない"})
                self.svc.emit("node.state", f"{nr['item_id']}/{nr['node_id']}", {"node": nr["node_id"], "state": "lost", "attempt": nr["attempt"]},
                              run_id=nr["run_id"] or None, node=nr["node_id"])
                rep["lost"].append(tag)
                if item and item["state"] in ("running", "planned"):
                    await self._eject(item, f"lost:{nr['node_id']}", todo_spec=None)
        # running のまま落ちた node_run（外への操作の前に落ちた）は、同じノードをやり直す
        for nr in self.store.unfinished_node_runs():
            self.store.finish_node_run(nr["id"], "interrupted", None, None, None)
            item = self.store.get_item(nr["item_id"])
            if item and item["state"] == "running" and item["item_id"] not in pending_results:
                pending_results[item["item_id"]] = (nr["node_id"], None)
        # 落ちた時に running だった項目を続ける
        for it in self.store.items(["running"]):
            if it["item_id"].startswith("run:") or it["item_id"] in self._tasks:
                continue
            node_id, res = pending_results.get(it["item_id"], (it.get("current_node"), None))
            if node_id is None:
                node_id = self._pipeline_start()
            if node_id is None:
                continue
            if res is not None:
                # 照合で決着した結果から次へ進む
                it = self._merge_artifacts(it, res.out)
                kind, target, edge = self._select_next(self.graph.nodes[node_id], res.status, res.out)
                if kind == "done":
                    self._item_state(it, "completed", None, artifacts=it.get("artifacts") or None)
                    continue
                if kind == "eject":
                    await self._eject(it, f"{node_id}:{res.status}", todo_spec=None)
                    continue
                node_id = target  # type: ignore[assignment]
                res = None
            self._tasks[it["item_id"]] = asyncio.create_task(self._run_item(it["item_id"], node_id, pending=res))
            rep["resumed"].append(it["item_id"])
        # 巡回の途中で落ちた run は failed にする（巡回は次の予定でやり直す。項目は作られていない）
        for it in self.store.items(["running"]):
            if it["item_id"].startswith("run:"):
                self.store.set_item_state(it["item_id"], "ejected", "interrupted")
                run = self.store.get_run(it["run_id"])
                if run and run["state"] in ("running", "launching", "planned"):
                    self._run_state(it["run_id"], "failed", "interrupted")
        return rep

    # ------------------------------------------------------------------ 取消
    async def cancel_item(self, item_id: str, reason: str = "cancelled") -> bool:
        it = self.store.get_item(item_id)
        if it is None or it["state"] in ("completed", "ejected"):
            return False
        t = self._tasks.get(item_id)
        if t is not None:
            t.cancel()
        self._item_state(it, "ejected", reason)
        return True

    # ------------------------------------------------------------------ 状態
    def status(self) -> dict:
        running = [i["item_id"] for i in self.store.items(["running"]) if not i["item_id"].startswith("run:")]
        last = None
        runs = self.store.runs()
        if runs:
            r = runs[-1]
            last = {"run_id": r["run_id"], "state": r["state"], "reason": r["reason"], "planned_at": r["planned_at"]}
        return {"mode": self.mode, "knobs": self.knobs(), "running": running, "queued": self.store.inbox_queued() + len(self.store.items(["planned"])),
                "last_run": last}


def _err(code: str, message: str = "", *, expected: bool = False) -> dict:
    return {"status": "error", "data": {}, "error": {"class": "system.expected" if expected else "unknown", "code": code, "message": message[:300]}}
