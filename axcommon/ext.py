"""拡張の読み込み（PLAN.json の conventions）。

部品（core/<名前>/、plugins/…）は、束ごとの拡張を `<部品>/ext/<束の ID>.py` に置く。各ファイルは `register(app)` を持つ。
共有ファイルの同時編集を避けるための差し込み口。load_extensions(app, ext_dir) が名前順に読み込み、register(app) を呼ぶ。

- `_` で始まるファイルと __init__.py は読まない。
- 1 つの拡張の読み込みや register の失敗は、その拡張だけを止める（fail-soft）。失敗は app.state.ext_failures に残り、
  axcommon.http の /healthz の `ext_failed` に出る。
- モジュールは importlib でファイルから読む（部品の名前にハイフンがあっても読める。例 core/memory-connector）。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("axcommon.ext")


def discover(ext_dir: str | Path) -> list[Path]:
    d = Path(ext_dir)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.py") if p.name != "__init__.py" and not p.name.startswith("_"))


def load_extensions(app: Any, ext_dir: str | Path | None, *, only: set[str] | None = None) -> dict[str, str | None]:
    """読み込んだ結果 {拡張名: None（成功）| エラーの型名} を返す。app.state.ext_failures に失敗の名前を足す。"""
    result: dict[str, str | None] = {}
    if ext_dir is None:
        return result
    state = getattr(app, "state", None)
    for path in discover(ext_dir):
        name = path.stem
        if only is not None and name not in only:
            continue
        mod_name = f"axext_{abs(hash(str(path.resolve()))):x}_{name}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("spec を作れない")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            reg = getattr(mod, "register", None)
            if not callable(reg):
                raise AttributeError("register(app) が無い")
            reg(app)
            result[name] = None
        except Exception as e:  # noqa: BLE001  拡張 1 件の失敗で部品を止めない
            sys.modules.pop(mod_name, None)
            result[name] = type(e).__name__
            log.error("拡張 %s の読み込みに失敗: %s", name, type(e).__name__)
            if state is not None:
                fails = getattr(state, "ext_failures", None)
                if fails is None:
                    fails = []
                    state.ext_failures = fails
                fails.append(name)
    return result
