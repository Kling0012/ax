"""グラフ検査の差し込み（S-5.7 の POST /v1/validate の kind=graph、D-4.1 の 2）。

`ax-graph-check`（CLI。CI で使う）と同じ関数を使う（S-5.7）。実体は B7 の束の `tools/ax_graph_check.py`。
この束は、その中の module 層の関数 `errors_for_body(body) -> list[str]`（graph を dict で受け、人の読める
エラーの一覧を返す）を動的に読む。まだ無ければ {ok: false, errors: ["graph 検査器が未実装"]} として
保存も登録もしない（B7 の完成で通る。OPEN.md の b3 の項）。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("core.control.graphcheck")

_CACHED: object | None = None
_CACHED_PATH: str | None = None


def graph_check_path() -> Path:
    env = os.environ.get("AX_GRAPH_CHECK")
    if env:
        return Path(env)
    return Path(os.environ.get("AX_HOME", "/ax")) / "tools" / "ax_graph_check.py"


def _load():
    global _CACHED, _CACHED_PATH
    p = graph_check_path()
    if not p.is_file():
        return None
    key = str(p)
    if _CACHED is not None and _CACHED_PATH == key:
        return _CACHED
    try:
        spec = importlib.util.spec_from_file_location("ax_graph_check_for_control", p)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001  検査器の失敗で制御 API を止めない
        log.exception("ax_graph_check の読み込みに失敗")
        return None
    _CACHED, _CACHED_PATH = mod, key
    return mod


def available() -> bool:
    return callable(getattr(_load(), "errors_for_body", None))


def errors_for_graph(body, *, root=None, subloop_dir=None) -> list[str]:
    """graph（dict）を検査する。検査器が無ければその旨を返す（未実装）。"""
    mod = _load()
    if mod is None:
        return ["graph 検査器がまだ無い（tools/ax_graph_check.py。B7 の束）"]
    fn = getattr(mod, "errors_for_body", None)
    if not callable(fn):
        return ["graph 検査器に errors_for_body(graph) が無い（B7 との約束。docs/b3/README.md）"]
    try:
        return list((fn(body, root=root, subloop_dir=subloop_dir) if subloop_dir is not None else fn(body)) or [])
    except Exception:  # noqa: BLE001
        log.exception("graph 検査で例外")
        return ["graph 検査器が例外を出した"]
