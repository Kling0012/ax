#!/usr/bin/env python3
"""ax-graph-check: サブループのグラフ検査（S-7.7、D-6.4、F-14.1 の CI の(2)）。制御 API の POST /v1/validate と同じ関数（runtime.subloop.graphcheck）を使う。

使い方:
  python3 tools/ax_graph_check.py plugins/subloop templates      ディレクトリを渡す
  python3 tools/ax_graph_check.py plugins/subloop/work-tdd       サブループ 1 つ（manifest.yaml と graph.yaml のあるディレクトリ）も可
  オプション: --root DIR（リポジトリの根。既定はこのファイルの親の親）、--json（結果を JSON で出す）
対象の探し方:
  - 渡したディレクトリ自身か、その直下か、`<dir>/subloop`（templates/ の下）に graph.yaml があれば、そのサブループ（manifest.yaml も検査する）。
  - `plugins/subloop/_lib/*.yaml` は部分グラフ。`include` で取り込んだ形に包んで、同じ検査を通す（取り込み側の既定のパラメータで）。
終了コード: 0=外れなし（警告はあってよい）、1=外れあり、2=使い方の誤り。登録の前に必須の検査なので、外れたグラフは登録しない（S-7.7）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent


def _targets(d: Path) -> tuple[list[Path], list[Path]]:
    """(サブループのディレクトリ, 部分グラフのファイル)"""
    subs: list[Path] = []
    libs: list[Path] = []
    if (d / "graph.yaml").is_file():
        subs.append(d)
    for c in sorted(p for p in d.iterdir() if p.is_dir()) if d.is_dir() else []:
        if c.name == "_lib":
            libs += sorted(c.glob("*.yaml"))
        elif (c / "graph.yaml").is_file():
            subs.append(c)
        elif c.name == "subloop" and (c / "graph.yaml").is_file():
            subs.append(c)
    return subs, libs


def errors_for_body(body, root: str | Path | None = None, *, subloop_dir: str | Path | None = None) -> list[str]:
    """制御 API（core/control/graphcheck.py）が動的に読む口。graph（dict）か {"graph":…, "manifest":…} を検査して、エラーの文の一覧を返す。"""
    sys.path.insert(0, str(Path(root or DEFAULT_ROOT).resolve()))
    from runtime.subloop import graphcheck

    return graphcheck.errors_for_body(body, root=root or DEFAULT_ROOT, subloop_dir=subloop_dir)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ax-graph-check")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    sys.path.insert(0, str(root))
    from axcommon.defaults import Defaults

    from runtime.subloop import graphcheck
    from runtime.subloop.graph import load_yaml
    from runtime.subloop.nodes import declared_node_dirs, load_registry

    try:
        defaults = Defaults.load()
    except Exception:  # noqa: BLE001
        defaults = None
    base_reg = load_registry(root / "plugins" / "nodes")
    results: list[dict] = []
    nerr = 0
    seen: set[Path] = set()
    for raw in a.paths:
        d = Path(raw)
        if not d.exists():
            print(f"ax-graph-check: パスが無い: {raw}", file=sys.stderr)
            return 2
        subs, libs = _targets(d.resolve())
        for s in subs:
            if s in seen:
                continue
            seen.add(s)
            own = [s / "nodes", *declared_node_dirs(s)]
            reg = load_registry(root / "plugins" / "nodes", *own) if any(p.is_dir() for p in own) else base_reg
            fs = graphcheck.check_subloop_dir(s, root=root, registry=reg, defaults=defaults)
            results.append({"target": str(s), "findings": [f.as_dict() for f in fs]})
        for lib in libs:
            if lib in seen:
                continue
            seen.add(lib)
            name = lib.stem
            wrapper = {"schema_version": 1, "entry": name, "nodes": [{"include": name, "as": name}], "edges": []}
            ml = {"roles": sorted(graphcheck.ROLE_TIERS), "sensitivity": "private", "outputs": sorted({t.meta["output"] for t in base_reg.types.values() if t.meta["output"]}),
                  "knobs": {"max_hops": {"type": "integer", "default": 20, "min": 2, "max": 20, "effective": "next_run", "title": "x"}}}
            try:
                load_yaml(lib)
                fs = graphcheck.check_graph(wrapper, ml, lib_dir=lib.parent, registry=base_reg, defaults=defaults)
            except Exception as e:  # noqa: BLE001
                fs = [graphcheck.Finding("lib_unreadable", f"{type(e).__name__}")]
            # 部分グラフ単体では、取り込み側が決める項目（item の項目）が未解決なのは正常。item.* と nodes.* の参照は検査済み。
            results.append({"target": str(lib), "findings": [f.as_dict() for f in fs]})
    for r in results:
        errs = [f for f in r["findings"] if f.get("severity", "error") == "error"]
        nerr += len(errs)
    if a.json:
        print(json.dumps({"ok": nerr == 0, "results": results}, ensure_ascii=False, indent=1))
    else:
        for r in results:
            errs = [f for f in r["findings"] if f.get("severity", "error") == "error"]
            warns = [f for f in r["findings"] if f.get("severity") == "warning"]
            mark = "NG" if errs else "OK"
            print(f"[{mark}] {r['target']}")
            for f in errs + warns:
                loc = f"（ノード {f['node']}）" if f.get("node") else ""
                print(f"    {f.get('severity', 'error')}: [{f['code']}] {f['message']}{loc}")
        if not results:
            print("ax-graph-check: 検査する対象が無い")
    return 0 if nerr == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
