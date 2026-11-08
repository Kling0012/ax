"""tests/b3 の共通の組立（T-4、T-183、T-5）。ネットワークとサービスを使わない。

- ledger / control の両方の口（中核 8080 相当・作業側 8081 相当）を Starlette の TestClient で試す。
  偽のコンテナを ax-work に置くのはこの形（F-18.4 の試験の T-183 と同じ考え。ソケットは使わない）。
- トークン: プラグインのトークン（hibi-state、memory-digest）と core-tokens（intake、hq、commander）を
  tmp_path の表に置く。秘密の値は試験のためにここだけで作る物（本物の秘密ではない）。
- 制御命令の発行者のトークン: control-token-manager などを tmp_path の秘密置き場に置く。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

pytest.importorskip("starlette.testclient")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

TOK_HIBI = "test-plugin-token-hibi-state-0001"
TOK_DIGEST = "test-plugin-token-memory-digest-01"
TOK_INTAKE = "test-core-token-intake-000000001"
TOK_HQ = "test-core-token-hq-000000000000001"
TOK_COMMANDER = "test-core-token-commander-000001"
TOK_MANAGER = "test-control-token-manager-00001"
TOK_BACKUP = "test-control-token-backup-000001"
TOK_INSPECTION = "test-control-token-inspection-1"
TOK_EMERGENCY = "test-control-token-emergency-001"
TOK_APPLY = "test-control-apply-token-0000001"


def sha256_of(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_table(path: Path, entries: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{name} {sha256_of(tok)}" for name, tok in sorted(entries.items())) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def b3_tokens(tmp_path, monkeypatch):
    """トークンの表と秘密置き場を作り、環境変数を向ける。戻り値はトークンの辞書。"""
    tokens = {"hibi-state": TOK_HIBI, "memory-digest": TOK_DIGEST}
    core = {"intake": TOK_INTAKE, "hq": TOK_HQ, "commander": TOK_COMMANDER}
    plugin_table = write_table(tmp_path / "plugin-tokens.sha256", tokens)
    core_table = write_table(tmp_path / "core-tokens.sha256", core)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    from core.control import ISSUER_TOKENS

    issuer_tokens = {"manager": TOK_MANAGER, "backup": TOK_BACKUP, "inspection": TOK_INSPECTION, "emergency": TOK_EMERGENCY}
    for issued_by, name in ISSUER_TOKENS.items():
        (secrets / name).write_text(issuer_tokens[issued_by], encoding="utf-8")
    (secrets / "control-apply-token").write_text(TOK_APPLY, encoding="utf-8")
    monkeypatch.setenv("AX_PLUGIN_TOKENS", str(plugin_table))
    monkeypatch.setenv("AX_CORE_TOKENS", str(core_table))
    monkeypatch.setenv("AX_SECRETS_PATH", str(secrets))
    yield {"plugins": tokens, "core": core, "issuers": issuer_tokens, "apply": TOK_APPLY,
           "plugin_table": plugin_table, "core_table": core_table, "secrets": secrets}


def fake_secret_scan(value):
    """S-15.3 の秘密検査の偽。F-25 の T-183 の (d) と同じ考えで `FICTIONAL` に当てる。"""
    text = repr(value)
    return ["FICTIONAL"] if "FICTIONAL" in text else []


@pytest.fixture
def ledger_env(tmp_path, fake_clock, b3_tokens, monkeypatch):
    """台帳の中核の口と作業側の口の TestClient。"""
    from axcommon.client import TimelineClient
    from axcommon.events import EventSink
    from core.ledger import LedgerService
    from core.ledger.app import create_core_app, create_work_app
    from core.ledger.store import Store
    from tests.fakes.http import FakeTransport

    transport = FakeTransport()
    transport.add("POST", "/v1/events", json={"ok": True})
    sink = EventSink(TimelineClient("http://timeline:8080", transport=transport), "ledger", "ci", tmp_path / "outbox")
    service = LedgerService(Store(tmp_path / "ledger" / "ledger.sqlite3"), sink, secret_scan=fake_secret_scan)
    core = TestClient(create_core_app(service))
    work = TestClient(create_work_app(service))
    yield {"service": service, "core": core, "work": work, "transport": transport, "clock": fake_clock,
           "tokens": b3_tokens, "tmp_path": tmp_path}
    service.store.close()


CONTROL_MANIFESTS = {
    "plugins/subloop/issue-automation/manifest.yaml": """\
schema_version: 1
id: issue-automation
version: 1.0.0
kind: subloop
project: dreamforge
title: GitHub の Issue を実装する
image: ax/runtime:1.0.0
execution: llm
schedule: {kind: interval, every_s: 300, missed: record_only}
graph: graph.yaml
capabilities: [run, collect_only, poll_paused, draining]
accepts_items: true
knobs:
  inbox_max: {type: integer, default: 50, min: 1, max: 100, effective: next_run, title: 受け取り待ちの上限}
  every_s: {type: integer, default: 300, min: 60, max: 3600, effective: next_run, title: 巡回の間隔（秒）}
  model: {type: enum, default: flash, enum: [free, flash, standard], effective: next_run, title: 既定の模型}
health: {heartbeat_s: 60}
sensitivity: private
""",
    "plugins/subloop/issue-automation/graph.yaml": """\
schema_version: 1
entry: check
nodes:
  - id: check
    type: shell.run
    with: {cmd: "true"}
    limits: {timeout_s: 30}
    on_fail: eject
edges: []
""",
    "plugins/resource/worker-pool/manifest.yaml": """\
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
  family_cap: {type: integer, default: 3, min: 1, max: 12, effective: immediate, title: 系統ごとの上限}
  free_min_success: {type: number, default: 0.7, min: 0.3, max: 0.9, effective: next_run, title: 無料模型の合格率, who: [user]}
health: {heartbeat_s: 60}
sensitivity: private
""",
}
REGISTRY_YAML = """\
schema_version: 1
plugins:
  - {id: issue-automation, kind: subloop, path: plugins/subloop/issue-automation/manifest.yaml}
  - {id: worker-pool, kind: resource, path: plugins/resource/worker-pool/manifest.yaml}
"""


@pytest.fixture
def control_env(tmp_path, fake_clock, b3_tokens, monkeypatch):
    """制御 API の中核の口と作業側の口の TestClient。登録簿と manifest は tmp_path に置く。"""
    from axcommon.client import TimelineClient, ApiClient
    from axcommon.events import EventSink
    from axcommon.creds import TokenTable, sha256_hex
    from core.resources.app import build as build_resources, Config as ResourceConfig
    from core.resources.core_auth import CoreAuth
    from core.control import ControlService
    from core.control.app import create_core_app, create_work_app
    from core.control.store import Store
    from tests.fakes.http import FakeTransport

    monkeypatch.setenv("AX_GRAPH_CHECK", str(Path(__file__).resolve().parents[2] / "tools" / "ax_graph_check.py"))
    root = tmp_path / "axroot"
    (root / "plugins" / "subloop" / "issue-automation").mkdir(parents=True)
    (root / "plugins" / "resource").mkdir(parents=True)
    for rel, body in CONTROL_MANIFESTS.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    (root / "plugins" / "registry.yaml").write_text(REGISTRY_YAML, encoding="utf-8")
    monkeypatch.setenv("AX_REGISTRY", str(root / "plugins" / "registry.yaml"))

    transport = FakeTransport()
    transport.add("POST", "/v1/events", json={"ok": True})
    sink = EventSink(TimelineClient("http://timeline:8080", transport=transport), "control", "ci", tmp_path / "outbox")
    resource_app, _, _ = build_resources(ResourceConfig(db=str(tmp_path / "limits-resources.sqlite")),
        auth=CoreAuth(TokenTable({"control": sha256_hex("limits-test")})), run_heartbeat_loop=False)
    resource_client = TestClient(resource_app)
    def resource_forward(call):
        response = resource_client.request(call.method, call.path + ("?" + call.query if call.query else ""),
                                           content=call.body, headers=call.headers)
        return response.status_code, dict(response.headers), response.content
    for method, path in (("GET", "/v1/resources"), ("GET", "/v1/slots/effective-limits"),
                         ("PUT", "/v1/slots/effective-limits")):
        transport.add(method, path, handler=resource_forward)
    service = ControlService(Store(tmp_path / "control" / "control.sqlite3"), sink,
                             applier_transport=transport, registry_root=root,
                             registry_file=root / "plugins" / "registry.yaml",
                             resources_client=ApiClient("http://resources:8080", token="limits-test", transport=transport))
    result = service.register()
    core = TestClient(create_core_app(service))
    work = TestClient(create_work_app(service))
    yield {"service": service, "core": core, "work": work, "transport": transport, "clock": fake_clock,
           "tokens": b3_tokens, "root": root, "register": result, "tmp_path": tmp_path}
    service.store.close()
