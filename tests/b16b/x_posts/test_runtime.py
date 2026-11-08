from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from axcommon import schemas, timeutil
from axcommon.ids import new_ulid
from runtime.subloop.graphcheck import check_graph
from runtime.subloop.nodes import load_registry
from x_posts import Draft
from x_posts.runtime import RuntimeBinding, prepare_manifest

PACKAGE = Path(__file__).resolve().parents[3] / "plugins" / "timer" / "x-posts"


@pytest.mark.parametrize("name", ["manifest.yaml", "draft.manifest.yaml"])
def test_manifests_and_graphs_match_b0_and_b7(name):
    manifest = yaml.safe_load((PACKAGE / name).read_text())
    schemas.validate("manifest", manifest)
    graph = yaml.safe_load((PACKAGE / manifest["graph"]).read_text())
    schemas.validate("graph", graph)
    registry = load_registry(PACKAGE / "nodes")
    assert not registry.failed
    assert "x.posts" in registry.names()
    findings = check_graph(graph, manifest, lib_dir=PACKAGE.parents[1] / "subloop" / "_lib", registry=registry)
    assert not findings, [str(finding) for finding in findings]
    for node in graph["nodes"]:
        assert node["needs_mode"] == (["run", "collect_only"] if node["id"] == "targets" else ["run"])


def test_jst_draft_time_is_translated_for_b7_utc_scheduler():
    manifest = yaml.safe_load((PACKAGE / "draft.manifest.yaml").read_text())
    assert prepare_manifest(manifest, {})["schedule"]["cron"] == "0 22 * * *"
    assert prepare_manifest(manifest, {"u_x_draft_time": "21:35"})["schedule"]["cron"] == "35 12 * * *"
    with pytest.raises(ValueError):
        prepare_manifest(manifest, {"u_x_draft_time": "25:00"})


def context(rig, manifest_name, mode="run"):
    manifest = yaml.safe_load((PACKAGE / manifest_name).read_text())
    binding = RuntimeBinding(rig.engine, accounts={rig.target.destination: rig.target.account})
    marked = []
    ctx = SimpleNamespace(svc=SimpleNamespace(x_posts_binding=binding), manifest=manifest,
        knobs={key: value["default"] for key, value in manifest["knobs"].items()},
        store=SimpleNamespace(get_run=lambda _: {"planned_at": timeutil.now_ts()}),
        run_id=new_ulid(), mode=mode, item={"data": {}}, marked=marked)

    async def call(fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def launching(key, ref):
        marked.append((key, ref))

    ctx.call, ctx.launching = call, launching
    return ctx


def test_b7_node_reports_unbound_runtime_without_sending():
    node = load_registry(PACKAGE / "nodes").get("x.posts")
    result = asyncio.run(node.run(SimpleNamespace(svc=SimpleNamespace(), knobs={}), {"with": {"action": "collect"}}))
    assert result["status"] == "error"


def test_b7_draft_and_send_nodes_use_real_x_engine(rig):
    rig.start()
    rig.worker.proposals = [Draft("draft", "demo", timeutil.now_ts())]
    ctx = context(rig, "draft.manifest.yaml")
    node = load_registry(PACKAGE / "nodes").get("x.posts")
    collected = asyncio.run(node.run(ctx, {"with": {"action": "collect", "phase": "draft"}}))
    assert rig.worker.calls == []
    assert collected["data"]["items"]
    ctx.item = {"data": collected["data"]["items"][0]}
    generated = asyncio.run(node.run(ctx, {"with": {"action": "draft"}}))
    assert generated["data"]["result"] == "drafted"
    assert rig.env.posts == []
    sent = asyncio.run(node.run(ctx, {"with": {"action": "send"}}))
    assert sent["data"]["state"] == "sent"
    assert ctx.marked[0][0].startswith("x-post:ax-x-")
    assert len(rig.env.posts) == 1


def test_b7_collect_only_daily_graph_stores_stats_without_items(rig):
    ctx = context(rig, "draft.manifest.yaml", mode="collect_only")
    node = load_registry(PACKAGE / "nodes").get("x.posts")
    result = asyncio.run(node.run(ctx, {"with": {"action": "collect", "phase": "draft"}}))
    assert result["data"]["items"] == []
    assert len(rig.env.calls) == 1
    assert rig.worker.calls == []
    assert rig.env.posts == []


def test_b7_queue_patrol_never_collects_statistics(rig):
    rig.start()
    ctx = context(rig, "manifest.yaml")
    result = asyncio.run(ctx.svc.x_posts_binding.collect(ctx, phase="queue"))
    assert result["data"]["items"]
    assert rig.env.calls == []
