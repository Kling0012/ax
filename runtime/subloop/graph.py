"""graph.yaml の読み込みと `include` の展開（S-7.1、D-6.4）。

展開は検査と実行の前に 1 回、機械的に行う。展開後は通常のグラフ（Graph）になる。
- 展開後のノード ID: `<as>.<元の ID>`（as を省くと部分グラフ名）。
- 取り込み側の辺 `to: <as>` は部分グラフの entry に、`from: <as>` と `when: X` は exits[X] のノードに結ぶ。
- `with` の値は部分グラフの中の `{{params.名前}}` に入る。パラメータは未指定なら既定値、既定値もなければ拒否する。
- 入れ子は深さ 3 まで。循環した include は拒否する。
- 部分グラフの中の `{{nodes.<局所 ID>...}}` と `on_fail: goto:<局所 ID>` は接頭辞つきに書き換える。
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import template

MAX_INCLUDE_DEPTH = 3
EDGE_WHEN = ("ok", "bug", "error", "skip")


class GraphError(ValueError):
    """グラフの形や include の誤り。args[0] は日本語の説明、code は機械が読む短い名前。"""

    def __init__(self, message: str, code: str = "graph_invalid"):
        super().__init__(message)
        self.code = code


@dataclass
class Edge:
    index: int
    src: str
    dst: str
    when: str = "ok"
    max_loops: int | None = None

    @property
    def key(self) -> str:
        return f"{self.index}:{self.src}->{self.dst}:{self.when}"


@dataclass
class Node:
    id: str
    type: str
    with_: dict
    limits: dict
    on_fail: str | None = None  # None = 指定なし（既定は eject）
    needs_mode: list[str] = field(default_factory=lambda: ["run"])
    origin: str | None = None  # 取り込んだ部分グラフ名（入れ子は外側から "a/b"）


@dataclass
class Graph:
    entry: str
    item_source: str | None
    max_parallel_items: Any
    nodes: dict[str, Node]
    edges: list[Edge]
    includes: list[str] = field(default_factory=list)  # 取り込んだ部分グラフ名（記録）

    def out_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.src == node_id]

    def edge_for(self, node_id: str, when: str) -> Edge | None:
        for e in self.edges:
            if e.src == node_id and e.when == when:
                return e
        return None


def load_yaml(path: str | Path) -> Any:
    import yaml

    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _node_from(d: dict, prefix: str | None, origin: str | None) -> Node:
    return Node(
        id=d["id"],
        type=d["type"],
        with_=copy.deepcopy(d.get("with") or {}),
        limits=copy.deepcopy(d.get("limits") or {}),
        on_fail=d.get("on_fail"),
        needs_mode=list(d.get("needs_mode") or ["run"]),
        origin=origin,
    )


_NODEREF = re.compile(r"(\{\{\s*nodes\.)([A-Za-z0-9_.\-]+)")


def _prefix_refs(value: Any, prefix: str, local_ids: set[str]) -> Any:
    """`{{nodes.<局所 ID>...}}` を `{{nodes.<prefix>.<局所 ID>...}}` に書き換える（最長の局所 ID を選ぶ）。"""
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            rest = m.group(2)
            parts = rest.split(".")
            for j in range(len(parts), 0, -1):
                cand = ".".join(parts[:j])
                if cand in local_ids:
                    return f"{m.group(1)}{prefix}.{rest}"
            return m.group(0)

        return _NODEREF.sub(sub, value)
    if isinstance(value, dict):
        return {k: _prefix_refs(v, prefix, local_ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_prefix_refs(v, prefix, local_ids) for v in value]
    return value


def _load_sub(lib_dir: Path, name: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]*", name or ""):
        raise GraphError(f"部分グラフの名前が不正: {name!r}", "include_unknown")
    path = lib_dir / f"{name}.yaml"
    if not path.is_file():
        raise GraphError(f"部分グラフ {name} が無い（{lib_dir}）", "include_unknown")
    doc = load_yaml(path)
    from axcommon import schemas

    errs = schemas.errors("subgraph", doc)
    if errs:
        raise GraphError(f"部分グラフ {name} の形が不正: {errs[0]}", "include_invalid")
    return doc


def _level(nodes_doc: list[dict], edges_doc: list[dict], entry_name: str | None, lib_dir: Path, depth: int, stack: tuple[str, ...],
           origin: str | None) -> tuple[dict[str, Node], list[dict], str | None, dict[str, tuple[str, dict[str, str]]], list[str]]:
    """1 階層を展開する。戻り: (ノード, 解決済みの辺, 解決済みの entry, この階層の as の表, 取り込んだ部分グラフ名)。

    辺の from/to は、この階層の名前空間のノード ID（取り込んだ部分グラフのノードは `<as>.<ID>`）に解決済み。
    """
    nodes: dict[str, Node] = {}
    edges: list[dict] = []
    sub_map: dict[str, tuple[str, dict[str, str]]] = {}
    used: list[str] = []
    for nd in nodes_doc:
        if "include" in nd:
            name = nd["include"]
            if name in stack:
                raise GraphError(f"include が循環している: {' -> '.join(stack + (name,))}", "include_cycle")
            if depth + 1 > MAX_INCLUDE_DEPTH:
                raise GraphError(f"include の深さが {MAX_INCLUDE_DEPTH} を超える: {name}", "include_depth")
            alias = nd.get("as") or name
            if alias in nodes or alias in sub_map:
                raise GraphError(f"ノード ID か as が重複: {alias}", "duplicate_id")
            sub = _load_sub(lib_dir, name)
            used.append(name)
            params_spec = sub.get("params") or {}
            given = nd.get("with") or {}
            for k in given:
                if k not in params_spec:
                    raise GraphError(f"部分グラフ {name} に無いパラメータ: {k}", "include_param")
            params: dict[str, Any] = {}
            for k, spec in params_spec.items():
                if k in given:
                    params[k] = given[k]
                elif isinstance(spec, dict) and spec.get("required") is True:
                    raise GraphError(f"部分グラフ {name} の必須パラメータが無い: {k}", "include_param")
                else:
                    params[k] = spec
            s_nodes, s_edges, s_entry, s_sub, s_used = _level(sub["nodes"], sub["edges"], sub["entry"], lib_dir, depth + 1, stack + (name,),
                                                             f"{origin}/{name}" if origin else name)
            used += s_used
            local_ids = set(s_nodes)
            for lid, n in s_nodes.items():
                nid = f"{alias}.{lid}"
                if nid in nodes:
                    raise GraphError(f"ノード ID が重複: {nid}", "duplicate_id")
                on_fail = n.on_fail
                if on_fail and on_fail.startswith("goto:"):
                    on_fail = f"goto:{alias}.{on_fail[5:]}"
                nodes[nid] = Node(id=nid, type=n.type, with_=template.render(_prefix_refs(n.with_, alias, local_ids), {"params": params}, partial=True),
                                  limits=template.render(n.limits, {"params": params}, partial=True), on_fail=on_fail,
                                  needs_mode=list(n.needs_mode), origin=n.origin)
            for se in s_edges:
                e2 = {"from": f"{alias}.{se['from']}", "to": f"{alias}.{se['to']}", "when": se["when"], "max_loops": se.get("max_loops")}
                if e2["max_loops"] is not None:
                    e2["max_loops"] = template.render(e2["max_loops"], {"params": params}, partial=True)
                edges.append(e2)
            exits: dict[str, str] = {}
            for k, v in sub["exits"].items():
                if v not in s_nodes:
                    raise GraphError(f"部分グラフ {name} の exits[{k}] が存在しないノードを指す: {v}", "include_invalid")
                exits[k] = f"{alias}.{v}"
            sub_map[alias] = (f"{alias}.{s_entry}", exits)
        else:
            if "id" not in nd:
                raise GraphError("ノードに id が無い", "graph_invalid")
            if nd["id"] in nodes or nd["id"] in sub_map:
                raise GraphError(f"ノード ID が重複: {nd['id']}", "duplicate_id")
            nodes[nd["id"]] = _node_from(nd, None, origin)
    # この階層の辺を解決する（as の名前を entry と出口へ）
    for e in edges_doc:
        src, dst = e["from"], e["to"]
        when = e.get("when", "ok")
        if dst in sub_map:
            dst = sub_map[dst][0]
        if src in sub_map:
            exits = sub_map[src][1]
            if when not in exits:
                raise GraphError(f"部分グラフ {src} に出口 {when} が無い（exits: {sorted(exits)}）", "include_exit")
            src = exits[when]
        if src not in nodes:
            raise GraphError(f"辺の from が存在しない: {e['from']}", "edge_unknown_node")
        if dst not in nodes:
            raise GraphError(f"辺の to が存在しない: {e['to']}", "edge_unknown_node")
        edges.append({"from": src, "to": dst, "when": when, "max_loops": e.get("max_loops")})
    entry = entry_name
    if entry in sub_map:
        entry = sub_map[entry][0]
    if entry is not None and entry not in nodes:
        raise GraphError(f"entry が存在しないノード: {entry}", "entry_unknown")
    return nodes, edges, entry, sub_map, used


def expand(doc: dict, lib_dir: str | Path, *, external_item_sources: set[str] | None = None) -> Graph:
    """Expand a graph, optionally allowing manifest-declared external item inputs."""
    nodes, raw_edges, entry, sub_map, used = _level(doc["nodes"], doc.get("edges") or [], doc["entry"], Path(lib_dir), 0, (), None)
    edges = [Edge(i, e["from"], e["to"], e["when"], e.get("max_loops")) for i, e in enumerate(raw_edges)]
    isrc = doc.get("item_source")
    if isrc in sub_map:
        raise GraphError("item_source に部分グラフは指定できない", "graph_invalid")
    allowed_external = external_item_sources or set()
    if isrc is not None and isrc not in nodes and isrc not in allowed_external:
        raise GraphError(f"item_source が存在しないノード: {isrc}", "item_source_unknown")
    return Graph(entry=entry, item_source=isrc, max_parallel_items=doc.get("max_parallel_items", 1), nodes=nodes, edges=edges, includes=used)
