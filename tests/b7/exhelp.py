"""実行系の試験の補助: 試験用ノード型（tests/fakes/b7/nodes/t）を使う小さなグラフを組む。"""
from __future__ import annotations

from pathlib import Path

from runtime.subloop.executor import Executor
from runtime.subloop.graph import expand
from runtime.subloop.nodes import load_registry
from tests.fakes.b7 import control

ROOT = Path(__file__).resolve().parents[2]
T_NODES = ROOT / "tests" / "fakes" / "b7" / "nodes"


def manifest(**over) -> dict:
    m = {"schema_version": 1, "id": "t", "version": "1.0.0", "kind": "subloop", "title": "t", "image": "x", "execution": "mechanical",
         "schedule": {"kind": "interval", "every_s": 60, "missed": "record_only"}, "capabilities": ["run", "collect_only", "poll_paused", "draining"],
         "health": {"heartbeat_s": 60}, "sensitivity": "private", "knobs": {"max_parallel_items": {"type": "integer", "default": 2, "effective": "next_run", "title": "x"},
                                                                          "max_hops": {"type": "integer", "default": 8, "effective": "next_run", "title": "x"}}}
    m.update(over)
    return m


def node(id_, type_="t.step", with_=None, timeout=30, **kw):
    n = {"id": id_, "type": type_, "with": with_ or {}, "limits": {"timeout_s": timeout, **kw.pop("limits", {})}}
    n.update(kw)
    return n


def graph(nodes, edges, *, entry=None, item_source=None, mpi=2):
    g = {"schema_version": 1, "entry": entry or nodes[0]["id"], "nodes": nodes, "edges": edges}
    if item_source:
        g["item_source"] = item_source
    g["max_parallel_items"] = mpi
    return g


def make_executor(env, g, man=None, store=None, reset=True) -> Executor:
    if reset:
        control.reset()
    reg = load_registry(ROOT / "plugins" / "nodes", T_NODES)
    ex = Executor(svc=env.svc, graph=expand(g, ROOT / "plugins" / "subloop" / "_lib"), manifest=man or manifest(), registry=reg, store=store or env.store)
    return ex


def patrol_graph(per_item_nodes, edges, items, *, mpi=2, list_needs_mode=None):
    """list(t.list) → per_item_nodes[0] → …（item_source=list）。"""
    lst = node("list", "t.list", {"items": items}, 30)
    if list_needs_mode:
        lst["needs_mode"] = list_needs_mode
    nodes = [lst] + per_item_nodes
    return graph(nodes, [{"from": "list", "to": per_item_nodes[0]["id"]}] + edges, entry="list", item_source="list", mpi=mpi)
