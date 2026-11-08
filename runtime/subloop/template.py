"""グラフの `{{...}}` の差し込み（S-7.1、S-7.2）。

- 全文が 1 つの `{{path}}` なら、値の型を保つ（`max: "{{knobs.max_parallel_items}}"` が整数になる）。
- 文の途中の `{{path}}` は文字列にして差し込む（None は空文字）。
- 参照できる根: knobs、item、nodes、params（部分グラフの展開のとき）。
- `nodes.<ID>.data.<項目>`: ノード ID は `tdd-change.worktree` のように点を含むので、存在する最長の ID を選ぶ。
- partial=True（部分グラフの展開）では、解決できない参照をそのまま残す。
"""
from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w.\-]*)\s*\}\}")
WHOLE = re.compile(r"^\s*\{\{\s*([A-Za-z_][\w.\-]*)\s*\}\}\s*$")


class TemplateError(ValueError):
    pass


_MISSING = object()


def references(value: Any) -> list[str]:
    """値の中のすべての参照パス（`knobs.repo` など）。"""
    out: list[str] = []
    if isinstance(value, str):
        out += [m.group(1) for m in PLACEHOLDER.finditer(value)]
    elif isinstance(value, dict):
        for v in value.values():
            out += references(v)
    elif isinstance(value, list):
        for v in value:
            out += references(v)
    return out


def _walk(obj: Any, parts: list[str]) -> Any:
    cur = obj
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return _MISSING
    return cur


def resolve(path: str, scope: dict[str, Any]) -> Any:
    """参照を解決する。解決できなければ _MISSING。"""
    parts = path.split(".")
    root = parts[0]
    if root not in scope:
        return _MISSING
    if root == "nodes":
        nodes = scope["nodes"] or {}
        # 最長の ID を選ぶ（ID が点を含むため）
        for j in range(len(parts), 1, -1):
            nid = ".".join(parts[1:j])
            if nid in nodes:
                return _walk(nodes[nid], parts[j:])
        return _MISSING
    return _walk(scope[root], parts[1:])


def _strict_missing(path: str, scope: dict[str, Any]) -> bool:
    # item.* と nodes.<ID>.data.* の欠けは None として扱う（任意の項目）。knobs と params の欠けは誤り
    root = path.split(".")[0]
    return root in ("knobs", "params")


def render(value: Any, scope: dict[str, Any], *, partial: bool = False) -> Any:
    if isinstance(value, str):
        m = WHOLE.match(value)
        if m:
            r = resolve(m.group(1), scope)
            if r is _MISSING:
                if partial:
                    return value
                if _strict_missing(m.group(1), scope) or m.group(1).split(".")[0] not in scope:
                    raise TemplateError(f"参照を解決できない: {m.group(1)}")
                return None
            return r

        def sub(mm: re.Match) -> str:
            rr = resolve(mm.group(1), scope)
            if rr is _MISSING:
                if partial:
                    return mm.group(0)
                if _strict_missing(mm.group(1), scope) or mm.group(1).split(".")[0] not in scope:
                    raise TemplateError(f"参照を解決できない: {mm.group(1)}")
                return ""
            return "" if rr is None else (rr if isinstance(rr, str) else str(rr))

        return PLACEHOLDER.sub(sub, value)
    if isinstance(value, dict):
        return {k: render(v, scope, partial=partial) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, scope, partial=partial) for v in value]
    return value
