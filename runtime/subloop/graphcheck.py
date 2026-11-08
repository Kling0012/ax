"""グラフ検査（S-7.7、D-6.4）。制御 API の `POST /v1/validate` と `ax-graph-check`（tools/ax_graph_check.py）が同じ関数を使う。

入口:
    check_graph(doc, manifest, lib_dir=…, registry=…)        -> list[Finding]   展開と S-7.7 の 1〜11
    check_manifest(manifest)                                   -> list[Finding]   S-4 の manifest の検査（スキーマ、knob の既定値が範囲内）
    check_request_bounds(manifest, request, via=…)             -> list[Finding]   S-7.7 の 12（subloop.create の依頼の宣言を超えない）
    validate_body(kind, body, root=…)                          -> {"ok", "errors": [...], "warnings": [...]}   POST /v1/validate の本体
    check_subloop_dir(dir, …)                                  -> list[Finding]   plugins/subloop/<id>/ 1 つ（ax-graph-check が使う）

実装の判断（docs/b7/README.md、DECISIONS.md に記録）:
- 検査 6 は、`bug` を返しうる型（META.statuses に bug を含む）のノードだけに求める（S-7.10 の例 `take`・`push` は on_fail を書かない）。
- `hop` は「その項目がすでに入ったノードへ入り直す回数」（辺の循環と goto）。必要な hop 数は、強連結成分ごとの max_loops の和の、最長経路の合計。
- 検査 9 の役割と区分の表は S-8.3 の候補の順（tier）による。config/roles.yaml（b6a）の形が決まったらそちらに替える。
"""
from __future__ import annotations

import importlib
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from . import template
from .graph import Graph, GraphError, expand, load_yaml
from .nodes import Registry, declared_node_dirs, load_registry, load_registry_into

IMPL_ROLES = ("impl", "impl_hard")
TDD_PREFIX = "tdd-change."
# S-8.3 の表。役割 → 候補に出る tier。区分 sensitive は standard 以上が要る（S-15.1）
ROLE_TIERS: dict[str, tuple[str, ...]] = {
    "eigen-decide": ("free", "flash"),
    "summary": ("free", "flash"), "research": ("free", "flash"), "second_opinion": ("free",), "screen": ("free", "flash"),
    "assist": ("flash", "free"), "test_design": ("flash", "free", "standard", "last_resort"), "impl": ("free", "standard"),
    "impl_hard": ("standard", "last_resort"), "review": ("flash", "standard"), "review_final": ("last_resort",),
    "manager": ("standard", "flash", "free"), "backup_manager": ("last_resort",), "impl_public": ("free",),
}
READONLY_ROLES = ("summary", "research", "second_opinion", "screen", "assist", "manager", "backup_manager")
SENSITIVITY_ORDER = ("public", "private", "sensitive")


@dataclass
class Finding:
    code: str
    message: str
    node: str | None = None
    severity: str = "error"

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}" + (f"（ノード {self.node}）" if self.node else "")


def _placeholder(v: Any) -> bool:
    return isinstance(v, str) and "{{" in v


def _with_errors(schema: dict, instance: Any) -> list[str]:
    import jsonschema

    out: list[str] = []
    try:
        v = jsonschema.Draft202012Validator(schema)
        for err in v.iter_errors(instance):
            if _placeholder(err.instance):
                continue  # 実行時に決まる値（差し込み）は型を確かめられない
            path = "/".join(str(p) for p in err.absolute_path)
            out.append(f"{path or '.'}: {err.message[:160]}")
    except jsonschema.SchemaError:
        out.append("型の with の Schema 自体が不正")
    return out


# ---- 構造 ----
def _adjacency(g: Graph, include_goto: bool = True) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {n: set() for n in g.nodes}
    for e in g.edges:
        adj[e.src].add(e.dst)
    if include_goto:
        for n in g.nodes.values():
            if n.on_fail and n.on_fail.startswith("goto:"):
                adj[n.id].add(n.on_fail[5:])
    return adj


def _reach(adj: dict[str, set[str]], start: str, banned: set[str] | None = None) -> set[str]:
    seen: set[str] = set()
    if banned and start in banned:
        return seen
    q = [start]
    seen.add(start)
    while q:
        x = q.pop(0)
        for y in adj.get(x, ()):
            if y not in seen and not (banned and y in banned):
                seen.add(y)
                q.append(y)
    return seen


def _sccs(adj: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan（再帰なし）。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in adj:
        if root in index:
            continue
        work = [(root, iter(sorted(adj[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            adv = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(adj[w]))))
                    adv = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if adv:
                continue
            work.pop()
            if work:
                u = work[-1][0]
                low[u] = min(low[u], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(comp)
    return out


def required_hops(g: Graph) -> int:
    """必要な最大 hop 数（再入場の回数の上限）。強連結成分ごとに内側の辺の max_loops の和、成分を DAG にして最長経路の合計。"""
    adj = _adjacency(g, include_goto=False)
    comps = _sccs(adj)
    comp_of = {n: i for i, c in enumerate(comps) for n in c}
    weight = [0] * len(comps)
    for e in g.edges:
        if comp_of[e.src] == comp_of[e.dst]:
            weight[comp_of[e.src]] += int(e.max_loops) if isinstance(e.max_loops, int) else 0
    dag: dict[int, set[int]] = {i: set() for i in range(len(comps))}
    for e in g.edges:
        a, b = comp_of[e.src], comp_of[e.dst]
        if a != b:
            dag[a].add(b)
    memo: dict[int, int] = {}

    def longest(i: int) -> int:
        if i in memo:
            return memo[i]
        memo[i] = 0  # 循環しない DAG だが念のため
        best = max((longest(j) for j in dag[i]), default=0)
        memo[i] = weight[i] + best
        return memo[i]

    return longest(comp_of[g.entry]) if g.entry in comp_of else 0


def _manifest_limit_hops(manifest: dict, defaults: Any | None) -> int:
    k = ((manifest or {}).get("knobs") or {}).get("max_hops")
    if isinstance(k, dict) and isinstance(k.get("default"), int):
        return k["default"]
    if defaults is not None:
        try:
            return int(defaults.get("max_hops"))
        except Exception:  # noqa: BLE001
            pass
    return 8


_TIER_CACHE: dict[str, dict[str, tuple[str, ...]]] = {}


def tier_table(root: Path | str | None) -> dict[str, tuple[str, ...]]:
    """役割 → 候補に出る tier。config/roles.yaml（b6a。S-8.3）があればそれを使い、読めなければ組み込みの表（ROLE_TIERS）。"""
    key = str(root)
    if key in _TIER_CACHE:
        return _TIER_CACHE[key]
    table = dict(ROLE_TIERS)
    try:
        import yaml

        if root is not None and (Path(root) / "config" / "roles.yaml").is_file():
            doc = yaml.safe_load((Path(root) / "config" / "roles.yaml").read_text(encoding="utf-8")) or {}
            for role, spec in (doc.get("roles") or {}).items():
                tiers: list[str] = []
                for p in spec.get("preference") or []:
                    head = re.split(r"[/(]", str(p), maxsplit=1)[0].strip()
                    tiers.append("last_resort" if head == "claude" else head)
                if tiers:
                    table[role] = tuple(tiers)
    except Exception:  # noqa: BLE001  読めなければ組み込みの表
        pass
    _TIER_CACHE[key] = table
    return table


def check_graph(doc: Any, manifest: dict | None, *, lib_dir: str | Path, registry: Registry,
                defaults: Any | None = None) -> list[Finding]:
    """manifest が None のときは、manifest に頼る検査（knob の宣言、outputs、roles の宣言、区分、hop の上限）を飛ばす（構造の検査だけ）。
    サブループ固有の型は registry の側に足す（`check_subloop_dir` が nodes/ と integration/register.py の register_graph_types を読む）。"""
    from axcommon import schemas

    root_dir = Path(lib_dir).resolve().parent.parent.parent

    have_manifest = manifest is not None
    manifest = manifest or {}
    m_outputs = set(manifest.get("outputs") or [])
    external_item_sources = m_outputs if manifest.get("accepts_items") is True else set()
    fs: list[Finding] = []
    # 1. スキーマ
    errs = schemas.errors("graph", doc)
    if errs:
        return [Finding("graph_schema", e) for e in errs[:20]]
    try:
        g = expand(doc, lib_dir, external_item_sources=external_item_sources)
    except GraphError as e:
        return [Finding(e.code, str(e))]

    knobs_decl = set((manifest.get("knobs") or {}).keys())
    m_roles = set(manifest.get("roles") or [])
    m_sens = manifest.get("sensitivity", "private")

    # 2. 型 と with の Schema
    for n in g.nodes.values():
        t = registry.get(n.type)
        if t is None:
            fs.append(Finding("unknown_type", f"型 {n.type} が部品庫に無い", n.id))
            continue
        for m in _with_errors(t.schema, n.with_):
            fs.append(Finding("with_schema", f"with が {n.type} の Schema に合わない: {m}", n.id))
    # 3. 到達
    adj = _adjacency(g)
    reach = _reach(adj, g.entry)
    for nid in g.nodes:
        if nid not in reach:
            fs.append(Finding("unreachable", "entry からたどり着けない", nid))
    # 4. すべての循環に max_loops がある: max_loops を持つ辺を取り除いたグラフに循環が残らない（S-7.7 の 4）。
    #    前向きの辺が、差し戻しの辺（max_loops 付き）と一緒に強連結成分に入るのは正常なので、成分の中の全部の辺には求めない。
    unb: dict[str, set[str]] = {n: set() for n in g.nodes}
    for e in g.edges:
        if not (isinstance(e.max_loops, int) and e.max_loops >= 1):
            unb[e.src].add(e.dst)
    for n in g.nodes.values():
        if n.on_fail and n.on_fail.startswith("goto:"):
            dst = n.on_fail[5:]
            if dst not in g.nodes:
                fs.append(Finding("goto_unknown", f"on_fail の goto 先 {dst} が無い", n.id))
            else:
                unb[n.id].add(dst)
    for comp in _sccs(unb):
        if len(comp) > 1 or comp[0] in unb[comp[0]]:
            fs.append(Finding("loop_without_max_loops", f"max_loops の無い循環がある: {' -> '.join(sorted(comp))}", sorted(comp)[0]))
    # 5. timeout_s
    for n in g.nodes.values():
        ts = (n.limits or {}).get("timeout_s")
        if not isinstance(ts, int) or ts < 1:
            fs.append(Finding("no_timeout", "limits.timeout_s が無い", n.id))
    # 6. 失敗時の行き先（bug を返しうる型）
    for n in g.nodes.values():
        t = registry.get(n.type)
        if t is None or not t.can_bug:
            continue
        has_bug_edge = any(e.src == n.id and e.when == "bug" for e in g.edges)
        if not has_bug_edge and n.on_fail is None:
            fs.append(Finding("no_failure_route", "when: bug の辺も on_fail も無い", n.id))
        if n.on_fail is not None and not re.fullmatch(r"eject|skip|goto:.+", n.on_fail):
            fs.append(Finding("on_fail_invalid", f"on_fail が不正: {n.on_fail}", n.id))
    # 辺の when が型の宣言と合う
    for e in g.edges:
        if e.when.startswith("outcome:"):
            t = registry.get(g.nodes[e.src].type)
            if t is not None and e.when[8:] not in t.meta["outcomes"]:
                fs.append(Finding("outcome_undeclared", f"型 {t.name} は結果 {e.when[8:]} を宣言していない", e.src))
    seen_pairs: set[tuple[str, str]] = set()
    for e in g.edges:
        if (e.src, e.when) in seen_pairs:
            fs.append(Finding("duplicate_edge", f"同じ (from, when) の辺が複数ある。最初の辺だけ使う: {e.src} when {e.when}", e.src, "warning"))
        seen_pairs.add((e.src, e.when))
    # 7. knob の宣言と参照の実在
    refs: list[tuple[str, str]] = []
    for n in g.nodes.values():
        for r in template.references(n.with_) + template.references(n.limits):
            refs.append((n.id, r))
    for r in template.references(g.max_parallel_items):
        refs.append(("", r))
    for nid, r in refs:
        root = r.split(".")[0]
        if root == "knobs":
            name = r.split(".")[1] if "." in r else ""
            if have_manifest and name not in knobs_decl:
                fs.append(Finding("knob_undeclared", f"{{{{knobs.{name}}}}} が manifest に宣言されていない", nid or None))
        elif root == "nodes":
            parts = r.split(".")
            if not any(".".join(parts[1:j]) in g.nodes for j in range(len(parts), 1, -1)):
                fs.append(Finding("node_ref_unknown", f"参照 {r} のノードが無い", nid or None))
        elif root == "params":
            fs.append(Finding("param_unresolved", f"{{{{{r}}}}} が展開されていない", nid or None))
        elif root != "item":
            fs.append(Finding("ref_unknown_root", f"参照の根が不明: {r}", nid or None))
    # 8. outputs と公開前の secret_scan
    scan_ids = {n.id for n in g.nodes.values() if n.type == "gate.secret_scan"}
    for n in g.nodes.values():
        t = registry.get(n.type)
        if t is None:
            continue
        if have_manifest and t.meta["output"] and t.meta["output"] not in m_outputs:
            fs.append(Finding("output_undeclared", f"外へ影響する型 {t.name} の操作 {t.meta['output']} が manifest の outputs に無い", n.id))
        if t.meta["publishes"]:
            if n.id in _reach(adj, g.entry, banned=scan_ids) and n.id not in scan_ids:
                fs.append(Finding("publish_without_secret_scan", f"外へ公開する型 {t.name} へ、gate.secret_scan を通らない経路がある", n.id))
    # 9. 役割と区分、読み取り専用の役割に workdir を渡さない
    for n in g.nodes.values():
        t = registry.get(n.type)
        if t is None or not t.meta["role_field"]:
            continue
        role = n.with_.get(t.meta["role_field"])
        if not isinstance(role, str) or _placeholder(role):
            continue
        if role not in ROLE_TIERS:
            fs.append(Finding("role_unknown", f"役割 {role} が S-8.3 に無い", n.id))
            continue
        if have_manifest and role not in m_roles:
            fs.append(Finding("role_undeclared", f"役割 {role} が manifest の roles に無い", n.id))
        tiers = tier_table(root_dir)[role]
        if have_manifest and m_sens == "sensitive" and not any(x in ("standard", "last_resort") for x in tiers):
            fs.append(Finding("sensitivity_conflict", f"区分 sensitive は standard 以上のモデルだけ。役割 {role} の候補（{'/'.join(tiers)}）に無い", n.id))
        if role in READONLY_ROLES and n.with_.get("workdir_id") not in (None, ""):
            fs.append(Finding("readonly_role_workdir", f"役割 {role} はリポジトリを書き換えられない。書き換えは work-tdd（tdd-change）だけ", n.id))
    # 10. 実装は tdd-change の中
    for n in g.nodes.values():
        t = registry.get(n.type)
        if t is None or not t.meta["role_field"]:
            continue
        role = n.with_.get(t.meta["role_field"])
        static_impl = isinstance(role, str) and role in IMPL_ROLES
        unknown_may_impl = _placeholder(role) and have_manifest and bool(m_roles & set(IMPL_ROLES))
        if (static_impl or unknown_may_impl) and not n.id.startswith(TDD_PREFIX):
            fs.append(Finding("impl_outside_tdd_change", "worker.run(impl / impl_hard) は tdd-change を通らなければならない（S-7.6）", n.id))
    # 11. 最大 hop 数
    need = required_hops(g)
    limit = _manifest_limit_hops(manifest, defaults)
    if have_manifest and need > limit:
        fs.append(Finding("hops_exceed", f"必要な最大 hop 数 {need} が manifest の上限 {limit} を超える"))
    return fs


def check_manifest(manifest: Any) -> list[Finding]:
    from axcommon import schemas

    errs = schemas.errors("manifest", manifest)
    fs = [Finding("manifest_schema", e) for e in errs[:20]]
    if fs or not isinstance(manifest, dict):
        return fs
    for name, k in (manifest.get("knobs") or {}).items():
        d, lo, hi = k.get("default"), k.get("min"), k.get("max")
        if isinstance(d, (int, float)) and not isinstance(d, bool):
            if (lo is not None and d < lo) or (hi is not None and d > hi):
                fs.append(Finding("knob_default_out_of_range", f"knob {name} の既定値 {d} が範囲 {lo}〜{hi} の外"))
    if manifest.get("kind") == "subloop" and not manifest.get("graph"):
        fs.append(Finding("manifest_graph_missing", "kind=subloop の manifest に graph が無い"))
    if manifest.get("accepts_items") and manifest.get("kind") != "subloop":
        fs.append(Finding("accepts_items_kind", "accepts_items は kind=subloop だけ"))
    if manifest.get("accepts_items") and "inbox_max" not in (manifest.get("knobs") or {}):
        fs.append(Finding("inbox_max_missing", "accepts_items のサブループは knob inbox_max を持つ（S-4.2）"))
    return fs


def check_request_bounds(manifest: dict, request: dict, *, via: str = "subloop.create") -> list[Finding]:
    """S-7.7 の 12。manifest の secrets/outputs/sensitivity が依頼の宣言を超えない。
    via='operate'（司令塔の受け口から operate で受けた依頼）は、さらに secrets が空、outputs が空、sensitivity が public。"""
    fs: list[Finding] = []
    req_secrets = set(request.get("secrets") or [])
    req_outputs = set(request.get("outputs") or [])
    req_sens = request.get("sensitivity", "public")
    if via == "operate":
        req_secrets, req_outputs, req_sens = set(), set(), "public"
    m_secrets = set(manifest.get("secrets") or [])
    m_outputs = set(manifest.get("outputs") or [])
    m_sens = manifest.get("sensitivity", "private")
    if not m_secrets <= req_secrets:
        fs.append(Finding("secrets_exceed_request", f"manifest の secrets が依頼の宣言を超える: {sorted(m_secrets - req_secrets)}"))
    if not m_outputs <= req_outputs:
        fs.append(Finding("outputs_exceed_request", f"manifest の outputs が依頼の宣言を超える: {sorted(m_outputs - req_outputs)}"))
    if req_sens not in SENSITIVITY_ORDER or m_sens not in SENSITIVITY_ORDER or SENSITIVITY_ORDER.index(m_sens) > SENSITIVITY_ORDER.index(req_sens):
        fs.append(Finding("sensitivity_exceeds_request", f"manifest の sensitivity {m_sens} が依頼の宣言 {req_sens} を超える"))
    return fs


def _split(fs: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    return [f for f in fs if f.severity == "error"], [f for f in fs if f.severity != "error"]


def _register_runtime_node_types(registry: Registry, d: Path, root: Path) -> Finding | None:
    """Add types exposed by a subloop's optional, side-effect-free graph hook."""
    register_file = d / "integration" / "register.py"
    if not register_file.is_file():
        return None
    try:
        module_name = ".".join(register_file.relative_to(root).with_suffix("").parts)
    except ValueError:
        return Finding("runtime_registry_unavailable", "実行時の型登録を root から読み込めない")
    root_text = str(root.resolve())
    added_path = root_text not in sys.path
    if added_path:
        sys.path.insert(0, root_text)
    try:
        module = importlib.import_module(module_name)
        hook = getattr(module, "register_graph_types", None)
        if hook is None:
            return None
        if not callable(hook):
            raise TypeError("register_graph_types が関数でない")
        hook(registry)
    except Exception as exc:  # noqa: BLE001  型の未登録を黙って通さない
        return Finding("runtime_registry_unavailable", f"実行時の型登録 {module_name} が使えない: {type(exc).__name__}")
    finally:
        if added_path:
            sys.path.remove(root_text)
    return None


def check_subloop_dir(d: str | Path, *, root: str | Path, registry: Registry | None = None, defaults: Any | None = None) -> list[Finding]:
    """plugins/subloop/<id>/ 1 つ（manifest.yaml と graph.yaml）。サブループ自身の nodes/ と、宣言された node_dirs も部品庫に足す。"""
    root, d = Path(root).resolve(), Path(d).resolve()
    fs: list[Finding] = []
    mpath, gpath = d / "manifest.yaml", d / "graph.yaml"
    manifest = None
    if mpath.is_file():
        manifest = load_yaml(mpath)
        fs += check_manifest(manifest)
    elif gpath.is_file():
        fs.append(Finding("manifest_missing", f"{d.name}: manifest.yaml が無い"))
    if gpath.is_file():
        if registry is None:
            reg = load_registry(root / "plugins" / "nodes", d / "nodes", *declared_node_dirs(d))
        else:
            # 渡された登録簿（実行時の register 済みなど）を残し、サブループ自身の nodes/ と宣言を足す。
            # 共通の部品庫は読み直さない（渡された登録簿に既にある）。
            reg = load_registry_into(Registry(types=dict(registry.types), failed=dict(registry.failed)),
                                     d / "nodes", *declared_node_dirs(d))
        registration_error = _register_runtime_node_types(reg, d, root)
        if registration_error is not None:
            fs.append(registration_error)
        fs += check_graph(load_yaml(gpath), manifest or {}, lib_dir=root / "plugins" / "subloop" / "_lib", registry=reg, defaults=defaults)
    return fs


def validate_body(kind: str, body: Any, *, root: str | Path, registry: Registry | None = None, defaults: Any | None = None) -> dict:
    """`POST /v1/validate`（F-14.3、S-5.7）の本体。何も保存しない。body は manifest か graph。
    graph のとき body は {"graph": {...}, "manifest": {...}} か graph 単体（manifest は無しで検査）。"""
    root = Path(root)
    if kind == "manifest":
        fs = check_manifest(body)
    elif kind == "graph":
        doc = body.get("graph") if isinstance(body, dict) and "graph" in body and "nodes" not in body else body
        manifest = body.get("manifest") if isinstance(body, dict) and "graph" in body and "nodes" not in body else None
        reg = registry or load_registry(root / "plugins" / "nodes")
        fs = check_graph(doc, manifest or {}, lib_dir=root / "plugins" / "subloop" / "_lib", registry=reg, defaults=defaults)
    else:
        return {"ok": False, "errors": [{"code": "kind_unknown", "message": f"kind は manifest か graph: {kind!r}"}], "warnings": []}
    errs, warns = _split(fs)
    return {"ok": not errs, "errors": [f.as_dict() for f in errs], "warnings": [f.as_dict() for f in warns]}


def errors_for_body(body: Any, *, root: str | Path | None = None, subloop_dir: str | Path | None = None) -> list[str]:
    """制御 API（core/control/graphcheck.py）が `tools/ax_graph_check.py` 経由で呼ぶ口。graph（dict）か {"graph":…, "manifest":…} を受け、
    人の読めるエラーの一覧を返す（空なら外れなし）。manifest が無いときは構造の検査だけ。"""
    import os

    base = Path(root or os.environ.get("AX_HOME_IN_CONTAINER") or os.environ.get("AX_HOME") or Path(__file__).resolve().parents[2])
    source_root = base.resolve()
    if not (base / "plugins" / "nodes").is_dir():
        base = Path(__file__).resolve().parents[2]
    wrapped = isinstance(body, dict) and "graph" in body and "nodes" not in body
    doc = body["graph"] if wrapped else body
    manifest = body.get("manifest") if wrapped else None
    reg = load_registry(base / "plugins" / "nodes")
    fs = []
    if subloop_dir is not None:
        d = Path(subloop_dir).resolve()
        expected = (source_root / "plugins").resolve()
        if d == expected or not d.is_relative_to(expected) or not isinstance(manifest, dict):
            return ["subloop のパスが不正"]
        declared = declared_node_dirs(d)
        if any(not p.is_relative_to(d) for p in declared):
            return ["node_dirs のパスが subloop の外にある"]
        if (d / "nodes").resolve().is_relative_to(d) is False or (d / "integration" / "register.py").resolve().is_relative_to(d) is False:
            return ["subloop 内のパスが不正"]
        reg = load_registry_into(reg, d / "nodes", *declared)
        registration_error = _register_runtime_node_types(reg, d, source_root)
        if registration_error is not None:
            fs.append(registration_error)
    fs += check_graph(doc, manifest, lib_dir=base / "plugins" / "subloop" / "_lib", registry=reg)
    return [str(f) for f in fs if f.severity == "error"]
