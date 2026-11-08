"""サービスの束（Services）と、ノードに渡す文脈（NodeContext）。D-6.2、D-6.3。

Services は外部との接点をまとめる。本番は runtime.subloop.config.build_services が環境変数から作り、試験は tests/fakes/b7 の偽を入れる。
クライアントは同期（axcommon.client）なので、ノードは `await ctx.call(fn, …)` で別スレッドから呼ぶ。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("ax.subloop")


@dataclass
class CmdResult:
    rc: int
    out: str = ""
    err: str = ""


@dataclass
class Services:
    plugin_id: str
    host: str
    sink: Any                       # axcommon.events.EventSink 互換: emit(kind, subject, data, **kw)
    pool: Any                       # WorkerPoolClient 互換: submit(job)、job(id)、cancel(id)
    ledger: Any                     # LedgerClient 互換: create_todo(todo)、get_todo(id)、list_todos(**params)
    control: Any                    # ControlClient 互換: effective(plugin_id)、validate(kind, body)
    timeline: Any                   # TimelineClient 互換: get_events(**params)
    defaults: Any                   # axcommon.defaults.Defaults
    work_root: Path                 # `work` のホスト bind（コンテナの中では /work）。worktree は work_root/job-<wd>/
    plugin_dir: Path                # plugins/subloop/<id>/
    lib_dir: Path                   # plugins/subloop/_lib/
    repo_root: Path                 # リポジトリの根（docs/test-procedure.md などを読む）
    resources: Any = None           # ApiClient 互換: 環境ジョブ（POST /v1/env/jobs）
    github: Any = None              # GitHub クライアント（runtime.subloop.github.GitHubClient 互換）
    git: Any = None                 # Git 実行（run(args, cwd=None) -> CmdResult。非同期）
    shell: Any = None               # シェル実行（run(cmd, cwd, timeout_s) -> CmdResult。非同期）
    errors: Any = None              # axcommon.errors.ErrorRules
    sleep: Callable[[float], Any] = asyncio.sleep
    remotes: dict[str, str] = field(default_factory=dict)   # git の remote 名 → URL かパス（例 work.git）
    repos: dict[str, str] = field(default_factory=dict)     # プロジェクト名 → リポジトリの URL かパス
    poll_s: float = 5.0             # worker-pool などの結果を待つ間隔

    def emit(self, kind: str, subject: str, data: dict | None = None, **kw: Any) -> dict | None:
        """事象を書く。書けなくても処理を止めない（S-2.2）。"""
        try:
            return self.sink.emit(kind, subject, data or {}, **kw)
        except Exception as e:  # noqa: BLE001
            log.error("事象 %s を書けない: %s", kind, type(e).__name__)
            return None


class NodeContext:
    def __init__(self, ex: Any, *, item: dict, node: Any, run_id: str, nr_id: int, attempt: int, continuation: int, deadline: str):
        self.ex = ex
        self.svc: Services = ex.svc
        self.store = ex.store
        self.item = item
        self.item_id: str = item["item_id"]
        self.node_id: str = node.id
        self.run_id = run_id
        self.nr_id = nr_id
        self.attempt = attempt
        self.continuation = continuation
        self.deadline = deadline
        self.knobs: dict[str, Any] = ex.knobs()
        self.mode: str = ex.mode
        self.defaults = self.svc.defaults
        self.manifest: dict = ex.manifest

    # ---- 呼び出し ----
    async def call(self, fn: Callable, *args: Any, **kw: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kw)

    async def sleep(self, s: float) -> None:
        await self.svc.sleep(s)

    def emit(self, kind: str, subject: str | None = None, data: dict | None = None) -> None:
        self.svc.emit(kind, subject or self.item_id, data or {}, run_id=self.run_id, node=self.node_id)

    # ---- 二重実行の防止（D-6.2）----
    async def launching(self, key: str, ref: dict | None = None) -> None:
        """外へ影響する操作の直前に必ず呼ぶ。node_runs と時系列記録に、照合に使う鍵を書く。"""
        self.store.set_launching(self.nr_id, key, ref)
        self.svc.emit("node.state", f"{self.item_id}/{self.node_id}",
                      {"node": self.node_id, "state": "launching", "attempt": self.attempt, "key": key},
                      run_id=self.run_id, node=self.node_id)

    def set_ref(self, ref: dict) -> None:
        self.store.set_ref(self.nr_id, ref)

    # ---- 参照 ----
    def previous_runs(self) -> list[dict]:
        """同じ項目の同じノードの、これまでの試行（今の物を除く）。worker.run の resume_from の元。"""
        return [r for r in self.store.node_runs(item_id=self.item_id, node_id=self.node_id) if r["id"] != self.nr_id]

    def outputs(self) -> dict[str, dict]:
        return self.store.node_outputs(self.item_id)

    def free_slots(self) -> int:
        return self.ex.free_slots()

    def workdir(self, workdir_id: str) -> Path:
        return Path(self.svc.work_root) / f"job-{workdir_id}"

    def read_prompt(self, rel: str) -> str:
        """prompt の置き場: サブループ自身の plugins/subloop/<id>/ → _lib/ の順。パスの外へ出る指定は拒否する。"""
        for base in (self.svc.plugin_dir, self.svc.lib_dir):
            p = (Path(base) / rel).resolve()
            if Path(base).resolve() in p.parents and p.is_file():
                return p.read_text(encoding="utf-8")
        raise FileNotFoundError(rel)
