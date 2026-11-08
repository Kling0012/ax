"""試験の環境一式（Services と偽）。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from axcommon.defaults import Defaults
from axcommon.errors import ErrorRules
from runtime.subloop.ctx import Services
from runtime.subloop.executor import Executor
from runtime.subloop.graph import expand, load_yaml
from runtime.subloop.nodes import load_registry
from runtime.subloop.store import Store

from .parts import FakeControl, FakeGit, FakeGitHub, FakeLedger, FakePool, FakeResources, FakeShell, FakeSink, FakeTimeline


@dataclass
class Env:
    root: Path
    svc: Services
    sink: FakeSink
    pool: FakePool
    ledger: FakeLedger
    github: FakeGitHub
    git: FakeGit
    shell: FakeShell
    resources: FakeResources
    timeline: FakeTimeline
    control: FakeControl
    store: Store
    work_root: Path

    def executor(self, graph_doc: dict, manifest: dict, *, extra_nodes: list | None = None, store: Store | None = None) -> Executor:
        reg = load_registry(self.root / "plugins" / "nodes", *(extra_nodes or []))
        g = expand(graph_doc, self.root / "plugins" / "subloop" / "_lib")
        return Executor(svc=self.svc, graph=g, manifest=manifest, registry=reg, store=store or self.store)

    def work_tdd(self, **manifest_over) -> Executor:
        d = self.root / "plugins" / "subloop" / "work-tdd"
        man = load_yaml(d / "manifest.yaml")
        man.update(manifest_over)
        self.svc.plugin_dir = d
        return self.executor(load_yaml(d / "graph.yaml"), man)


def make_env(tmp_path: Path, repo_root: Path, clock=None) -> Env:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    sink = FakeSink()
    pool, ledger, github, git, shell = FakePool(work), FakeLedger(), FakeGitHub(), FakeGit(), FakeShell()
    resources, timeline, control = FakeResources(), FakeTimeline(), FakeControl()

    async def _sleep(s):
        if clock is not None:
            clock.sleep(s)
        await asyncio.sleep(0)

    svc = Services(plugin_id="work-tdd", host="test-host", sink=sink, pool=pool, ledger=ledger, control=control, timeline=timeline,
                   defaults=Defaults.load(), work_root=work, plugin_dir=repo_root / "plugins" / "subloop" / "work-tdd",
                   lib_dir=repo_root / "plugins" / "subloop" / "_lib", repo_root=repo_root, resources=resources, github=github, git=git, shell=shell,
                   errors=ErrorRules.load(), sleep=_sleep, remotes={"WORK_GIT": "/git/work.git"}, repos={"ax": "/git/ax.git", "demo": "/git/demo.git"}, poll_s=1.0)
    return Env(repo_root, svc, sink, pool, ledger, github, git, shell, resources, timeline, control, Store(":memory:"), work)
