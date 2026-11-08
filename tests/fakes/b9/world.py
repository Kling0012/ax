"""B9/B11/B13 の試験の組立。本物の timeline・control・ledger（B1・B3）を 1 つの偽の網（FaultNet）に載せ、
管理役・点検係・プラグインの実行系（B7）を同じ網につなぐ。モデルは FakePool（b0 の FakeModelGateway）。ソケットを使わない。

  w = World(tmp_path, repo_root, fake_clock, monkeypatch, plugin_ids=("pa", "pb"))
  w.manager()            管理役（Manager）。w.inspector() 点検係。どちらも FaultNet 経由で本物の API を呼ぶ
  w.net                  FaultNet（stop/delay/respond/lose_response）。w.pool.gateway.script(...) で返答を決める
  w.emit(kind, subject, data, component=…)    時系列記録（本物）へ事象を足す。w.events(kind)    読む
  w.add_plugin_runtime("pa")                  B7 の実行系をプラグインとして網に載せる（sl-pa:8080）
starlette・httpx・jsonschema が要る（無い環境では importorskip で飛ばす）。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from axcommon import creds
from axcommon.client import ApiClient, TimelineClient
from axcommon.events import EventSink
from axcommon.redact import Redactor
from tests.fakes.fault import FaultNet
from tests.fakes.model_gateway import FakeModelGateway

from .fake_pool import FakePool

TOK = {
    "manager": "test-control-token-manager-00001", "backup": "test-control-token-backup-000001",
    "inspection": "test-control-token-inspection-1", "emergency": "test-control-token-emergency-001",
    "apply": "test-control-apply-token-0000001", "core-manager": "test-core-token-manager-0000001",
    "core-intake": "test-core-token-intake-000000001",
}

MANIFEST = """\
schema_version: 1
id: {pid}
version: 1.0.0
kind: subloop
project: demo
title: 試験のサブループ {pid}
image: ax/runtime:1.0.0
execution: mechanical
schedule: {{kind: interval, every_s: 300, missed: record_only}}
graph: graph.yaml
capabilities: [run, collect_only, poll_paused, draining]
accepts_items: false
knobs:
  every_s: {{type: integer, default: 300, min: 60, max: 3600, effective: next_run, title: 巡回の間隔（秒）}}
  max_parallel_items: {{type: integer, default: 2, min: 1, max: 8, effective: next_run, title: 並列の項目の上限}}
  model: {{type: enum, default: flash, enum: [free, flash, standard], effective: next_run, title: 既定の模型}}
health: {{heartbeat_s: 60}}
sensitivity: private
"""
POOL_MANIFEST = """\
schema_version: 1
id: worker-pool
version: 1.0.0
kind: resource
title: モデルのワーカー層
image: ax/runtime:1.0.0
execution: mechanical
schedule: {kind: event, missed: record_only}
capabilities: [run, collect_only, poll_paused, draining]
knobs:
  family_cap: {type: integer, default: 0, min: 0, max: 12, effective: immediate, title: 系統ごとの上限}
health: {heartbeat_s: 60}
sensitivity: private
"""


def sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


class FakeResources:
    def __init__(self):
        self.items: list[dict] = []

    def models(self):
        return list(self.items)


class FakeMemory:
    def __init__(self):
        self.remembered: list[dict] = []
        self.fail = False

    def remember(self, body):
        from axcommon.client import Unavailable

        if self.fail:
            raise Unavailable("down")
        self.remembered.append(body)
        return {"memory_id": "m1"}


class World:
    def __init__(self, tmp_path: Path, repo_root: Path, clock, monkeypatch, plugin_ids=("pa", "pb")):
        import pytest

        pytest.importorskip("starlette.testclient")
        pytest.importorskip("httpx")
        from starlette.testclient import TestClient

        from core.control import ControlService, ISSUER_TOKENS
        from core.control import app as control_app
        from core.control.store import Store as ControlStore
        from core.ledger import LedgerService
        from core.ledger import app as ledger_app
        from core.ledger.store import Store as LedgerStore
        from core.timeline import app as tl_app
        from core.resources.app import Config as ResourcesConfig, build as build_resources
        from core.resources.core_auth import CoreAuth
        from core.control.selection_exclusions import ExclusionAdapter, ResourcesExclusionClient
        import yaml

        self.tmp, self.root, self.clock = tmp_path, repo_root, clock
        self.plugin_ids = list(plugin_ids)
        self.net = FaultNet(clock=clock)
        self.gateway = FakeModelGateway(api_key=None)
        self.pool = FakePool(self.gateway, clock)
        self.resources, self.memory = FakeResources(), FakeMemory()

        # 秘密と資格（試験のための作り物）
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        for issued_by, fname in ISSUER_TOKENS.items():
            (secrets / fname).write_text(TOK[issued_by], encoding="utf-8")
        (secrets / "control-apply-token").write_text(TOK["apply"], encoding="utf-8")
        core_table = tmp_path / "core-tokens.sha256"
        core_table.write_text(f"intake {sha(TOK['core-intake'])}\nmanager {sha(TOK['core-manager'])}\n", encoding="utf-8")
        plugin_table = tmp_path / "plugin-tokens.sha256"
        plugin_table.write_text("".join(f"{p} {sha('tok-' + p)}\n" for p in self.plugin_ids), encoding="utf-8")
        monkeypatch.setenv("AX_SECRETS_PATH", str(secrets))
        monkeypatch.setenv("AX_CORE_TOKENS", str(core_table))
        monkeypatch.setenv("AX_PLUGIN_TOKENS", str(plugin_table))

        # 本物の時系列記録
        cfg = tl_app.Config(host="ci", db=str(tmp_path / "tl.sqlite"), archive_dir=str(tmp_path / "tl-archive"), redact_index=None, pii_list=None,
                            export_interval_s=0, hot_days=30, longpoll_s=5)
        tl_core, tl_work, self.timeline = tl_app.build(cfg, redactor=Redactor(), tokens=creds.TokenTable({p: sha("tok-" + p) for p in self.plugin_ids}),
                                                   run_heartbeat_loop=False)
        self.tl_client = TestClient(tl_core)
        self.net.add("timeline", self.tl_client, port=8080)
        self.net.add("timeline", TestClient(tl_work), port=8081)

        # 実際の resources API と control 用の試験資格を使う。
        models = tmp_path / "models.yaml"
        models.write_text(yaml.safe_dump({"models": [{"id": "model-test", "kind": "model",
            "family": "test", "tier": "standard", "billing": "flat", "level": "L1",
            "vision": False, "runner": "agent-cli", "status": "active"}]}), encoding="utf-8")
        control_token = "test-resources-control-token"
        auth = CoreAuth(creds.TokenTable({"control": sha(control_token)}))
        resources_app, _, self.resources_service = build_resources(
            ResourcesConfig(db=str(tmp_path / "resources.sqlite"), models_file=str(models)),
            auth=auth, run_heartbeat_loop=False)
        self.net.add("resources", TestClient(resources_app), port=8080)
        resources_client = ApiClient("http://resources:8080", control_token,
                                     transport=self.net.transport("control"))

        # 本物の制御 API（登録簿と manifest は tmp_path）
        root = tmp_path / "axroot"
        for pid in self.plugin_ids:
            d = root / "plugins" / "subloop" / pid
            d.mkdir(parents=True)
            (d / "manifest.yaml").write_text(MANIFEST.format(pid=pid), encoding="utf-8")
            (d / "graph.yaml").write_text("schema_version: 1\nentry: check\nnodes:\n  - id: check\n    type: shell.run\n"
                                          "    with: {cmd: 'true'}\n    limits: {timeout_s: 30}\n    on_fail: eject\nedges: []\n", encoding="utf-8")
        d = root / "plugins" / "resource" / "worker-pool"
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text(POOL_MANIFEST, encoding="utf-8")
        reg = ["schema_version: 1", "plugins:"] + [f"  - {{id: {p}, kind: subloop, path: plugins/subloop/{p}/manifest.yaml}}" for p in self.plugin_ids]
        reg.append("  - {id: worker-pool, kind: resource, path: plugins/resource/worker-pool/manifest.yaml}")
        (root / "plugins" / "registry.yaml").write_text("\n".join(reg) + "\n", encoding="utf-8")
        monkeypatch.setenv("AX_REGISTRY", str(root / "plugins" / "registry.yaml"))
        self.axroot = root
        self.control = ControlService(ControlStore(tmp_path / "control.sqlite3"),
                                      EventSink(TimelineClient("http://timeline:8080", transport=self.net.transport("control")), "control", "ci",
                                                tmp_path / "control-outbox"),
                                      applier_transport=self.net.transport("control", zone="work"), registry_root=root,
                                      registry_file=root / "plugins" / "registry.yaml", resources_client=resources_client,
                                      exclusion_adapter=ExclusionAdapter(ResourcesExclusionClient(resources_client)))
        monkeypatch.setenv("AX_GRAPH_CHECK", str(repo_root / "tools" / "ax_graph_check.py"))
        registration = self.control.register()
        assert not registration["rejected"], registration["rejected"]
        assert "worker-pool" in registration["registered"]
        limits = resources_client.get("/v1/slots/effective-limits")
        assert limits["revision"] >= 2
        slots = resources_client.get("/v1/slots")
        assert slots["models"]["model-test"]["cap"] == 1
        self.net.add("control", TestClient(control_app.create_core_app(self.control)), port=8080)
        self.net.add("control", TestClient(control_app.create_work_app(self.control)), port=8081)

        # 本物の台帳
        self.ledger = LedgerService(LedgerStore(tmp_path / "ledger.sqlite3"),
                                    EventSink(TimelineClient("http://timeline:8080", transport=self.net.transport("ledger")), "ledger", "ci",
                                              tmp_path / "ledger-outbox"))
        self.net.add("ledger", TestClient(ledger_app.create_core_app(self.ledger)), port=8080)
        self.runtimes: dict[str, dict] = {}
        self.net.apply_f184()        # F-18.4 の到達表（ax-work から ax-core へ届かない。作業側の口は 8081）

    # ---- 部品 ----
    def clients(self, who: str):
        from core.manager.clients import Clients, ManagerControl, ManagerLedger

        t = self.net.transport(who)
        tok_ctl = TOK["manager"] if who == "manager" else TOK["inspection"]
        return Clients(timeline=TimelineClient("http://timeline:8080", transport=t),
                       control=ManagerControl("http://control:8080", tok_ctl, transport=t),
                       ledger=ManagerLedger("http://ledger:8080", TOK["core-manager"], transport=t),
                       pool=self.pool, resources=self.resources, memory=self.memory)

    def manager(self, **cfg_over):
        from axcommon.defaults import Defaults
        from core.manager.config import ManagerConfig
        from core.manager.cycle import Manager
        from core.manager.llm import PoolLLM
        from core.manager.store import Store

        cfg = ManagerConfig.load(Defaults.load(), environ={"AX_HOST": "ci"})
        cfg.poll_s = 5.0
        for k, v in cfg_over.items():
            setattr(cfg, k, v)
        cl = self.clients("manager")
        llm = PoolLLM(self.pool, poll_s=cfg.poll_s, sleep=self.clock.sleep, monotonic=self.clock.monotonic)
        sink = EventSink(cl.timeline, "manager", "ci", self.tmp / "mgr-outbox")
        return Manager(cl, Store(":memory:"), sink, cfg, llm, outbox_dir=self.tmp / "mgr-ledger-outbox")

    def inspector(self, **cfg_over):
        from axcommon.defaults import Defaults
        from core.inspector.config import InspectionConfig
        from core.inspector.inspector import Inspector
        from core.inspector.state import StateStore

        cfg = InspectionConfig.load(self.root / "config" / "inspection.yaml", Defaults.load(), environ={"AX_HOST": "ci"})
        for k, v in cfg_over.items():
            setattr(cfg, k, v)
        cl = self.clients("inspector")
        sink = EventSink(cl.timeline, "inspector", "ci", self.tmp / "insp-outbox")
        return Inspector(cl, StateStore(self.tmp / "insp-state"), sink, cfg)

    # ---- 事象 ----
    def emit(self, kind: str, subject: str, data: dict | None = None, component: str = "test", **kw) -> dict:
        from axcommon.events import build_event

        ev = build_event(component=component, host="ci", kind=kind, subject=subject, data=data or {}, **kw)
        TimelineClient("http://timeline:8080", transport=self.net.transport(component)).post_events(ev)
        return ev

    def events(self, kind: str | None = None, component: str | None = None) -> list[dict]:
        out = self.timeline.store.list_events(after=0, kinds=[kind] if kind else None, limit=100000)
        return [e for e in out if component is None or e["source"]["component"] == component]

    # ---- 命令 ----
    def command(self, issued_by: str, **cmd) -> dict:
        from axcommon.client import ApiClient

        c = ApiClient("http://control:8080", TOK[issued_by], transport=self.net.transport(issued_by))
        return c.post("/v1/commands", cmd)

    # ---- B7 の実行系をプラグインとして載せる ----
    def add_plugin_runtime(self, pid: str, items: int = 2, *, sleep_s: float = 0.0, mpi: int = 2):
        """B7 の Executor を 1 つ組み、`sl-<pid>:8080` として網に載せる（/apply と /status の橋。control の資格を確かめる）。
        事象は作業側の口（timeline:8081、プラグインの資格）へ書く。届かなければ outbox/ に溜まる。"""
        from tests.b7.exhelp import make_executor, node, patrol_graph
        from tests.fakes.b7 import make_env

        rt_dir = self.tmp / f"rt-{pid}"
        rt_dir.mkdir(exist_ok=True)
        env = make_env(rt_dir, self.root, self.clock)
        env.svc.plugin_id = pid
        tl = TimelineClient("http://timeline:8081", "tok-" + pid, transport=self.net.transport(pid, zone="work"))
        sink = EventSink(tl, pid, "ci", rt_dir / "outbox", subloop=pid)
        env.svc.sink = sink
        its = [{"id": f"{pid}-{chr(65 + i)}"} for i in range(items)]
        steps = [node("s1", "t.sleep", {"seconds": sleep_s}), node("s2", "t.step", {"status": "ok"}), node("s3", "t.step", {"status": "ok"})]
        g = patrol_graph(steps, [{"from": "s1", "to": "s2"}, {"from": "s2", "to": "s3"}], its, mpi=mpi)
        man = {"schema_version": 1, "id": pid, "version": "1.0.0", "kind": "subloop", "title": pid, "image": "x", "execution": "mechanical",
               "schedule": {"kind": "interval", "every_s": 300, "missed": "record_only"},
               "capabilities": ["run", "collect_only", "poll_paused", "draining"], "health": {"heartbeat_s": 60}, "sensitivity": "private",
               "knobs": {"max_parallel_items": {"type": "integer", "default": mpi, "effective": "next_run", "title": "x"},
                         "max_hops": {"type": "integer", "default": 8, "effective": "next_run", "title": "x"}}}
        ex = make_executor(env, g, man=man, reset=False)
        self.net.add(f"sl-{pid}", RuntimeBridge(ex, TOK["apply"]), port=8080)
        self.runtimes[pid] = {"ex": ex, "env": env, "items": its, "sink": sink}
        return ex

    def poll_effective(self, pid: str) -> None:
        """プラグインが control_poll_s ごとに effective を読んで合わせる動き（S-4.4。/apply が届かなくても合う）。"""
        eff = self.control.effective(pid)
        self.runtimes[pid]["ex"].apply(eff["mode"], eff.get("knobs"), eff.get("revision"))


class RuntimeBridge:
    """sl-<id>:8080 の /apply と /status を B7 の Executor へつなぐ橋（Transport 互換）。資格 control-apply-token だけを受ける（F-18.4）。"""

    def __init__(self, ex, apply_token: str):
        self.ex, self.token = ex, apply_token

    def __call__(self, method, url, headers, body, timeout):
        import json
        from urllib.parse import urlsplit

        path = urlsplit(url).path
        auth = {k.lower(): v for k, v in headers.items()}.get("authorization", "")

        def out(status, obj):
            return status, {"Content-Type": "application/json"}, json.dumps(obj).encode()

        if method == "POST" and path == "/apply":
            if auth != "Bearer " + self.token:
                return out(401, {"error": {"code": "unauthorized", "message": "control の資格が要る"}})
            d = json.loads(body.decode())
            applied = self.ex.apply(d["mode"], d.get("knobs"), d["revision"])
            return out(200, {"ok": True, "applied": applied, "revision": self.ex.revision})
        if method == "GET" and path == "/status":
            return out(200, self.ex.status())
        if path == "/healthz":
            return out(200, {"ok": True, "component": self.ex.svc.plugin_id})
        return out(404, {"error": {"code": "not_found", "message": ""}})
