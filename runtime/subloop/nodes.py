"""部品庫の型の読み込み（D-6.3、S-7.4）。

`plugins/nodes/<名前空間>/<型>.py` が型 `<名前空間>.<型>` になる。サブループ自身が持つ型は `plugins/subloop/<id>/nodes/<名前空間>/<型>.py`
（後続の束の差し込み口。部品庫と同じ名前は置けない）。読み込みに失敗した型はその型だけ使えない（fail-soft）。失敗は Registry.failed に残る。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .nodeapi import merge_meta

log = logging.getLogger("ax.nodes")


@dataclass
class NodeType:
    name: str
    path: str
    schema: dict
    run: Callable
    reconcile: Callable | None
    meta: dict

    @property
    def can_bug(self) -> bool:
        return "bug" in self.meta["statuses"]


@dataclass
class Registry:
    types: dict[str, NodeType] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> NodeType | None:
        return self.types.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self.types

    def names(self) -> list[str]:
        return sorted(self.types)


def _load_module(path: Path, name: str):
    mod_name = f"axnode_{abs(hash(str(path.resolve()))):x}_{name.replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("spec を作れない")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return mod


def load_registry(*dirs: str | Path) -> Registry:
    """dirs（`plugins/nodes` と、サブループ自身の `nodes/`）を順に読む。同じ型名が 2 つあれば後の物を失敗として残す。"""
    reg = Registry()
    for d in dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        for ns_dir in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
            for f in sorted(ns_dir.glob("*.py")):
                if f.name.startswith("_"):
                    continue
                tname = f"{ns_dir.name}.{f.stem}"
                try:
                    mod = _load_module(f, tname)
                    if getattr(mod, "NODE_HELPER", False):
                        continue
                    tname = getattr(mod, "TYPE", tname)  # `notify` のように名前空間のない型は TYPE で名乗る
                    if tname in reg.types:
                        reg.failed[tname] = "duplicate"
                        continue
                    schema = getattr(mod, "schema")
                    run = getattr(mod, "run")
                    if not isinstance(schema, dict) or not callable(run):
                        raise TypeError("schema(dict) と run(ctx, inp) が要る")
                    meta = merge_meta(getattr(mod, "META", None))
                    rec = getattr(mod, "reconcile", None)
                    if meta["launching"] and not callable(rec):
                        raise TypeError("launching の型は reconcile(ctx, key) が要る")
                    reg.types[tname] = NodeType(tname, str(f), schema, run, rec if callable(rec) else None, meta)
                except Exception as e:  # noqa: BLE001  1 つの型の失敗で部品庫を止めない
                    reg.failed[tname] = type(e).__name__
                    log.error("型 %s の読み込みに失敗: %s", tname, type(e).__name__)
    return reg


def load_registry_into(reg: Registry, *dirs: str | Path) -> Registry:
    """reg に dirs の型を足す。同じ型名は先にある物が残り、後から来た物は duplicate として失敗に残る（load_registry と同じ規則）。"""
    for d in dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        loaded = load_registry(base)
        for name, t in loaded.types.items():
            if name in reg.types:
                reg.failed[name] = "duplicate"
            else:
                reg.types[name] = t
        for name, why in loaded.failed.items():
            if name not in reg.types and name not in reg.failed:
                reg.failed[name] = why
    return reg


def declared_node_dirs(plugin_dir: str | Path) -> list[Path]:
    """サブループが宣言する型の追加の置き場（D-6.3 の差し込み口）。

    `nodes.yaml`（サブループのディレクトリ、またはその直下の1階層。例 integration/nodes.yaml）に
    `node_dirs:`（このファイルの置き場から相対のパスの list）を書く。パーミッションの都合で
    `nodes/` を置けないサブループ（例 change の integration/）が、自分の担当パスの中で
    型の置き場を宣言するための物。宣言が無ければ空。1 つの宣言の読み込みの失敗は、その宣言だけを飛ばす（fail-soft）。
    """
    d = Path(plugin_dir)
    out: list[Path] = []
    seen: set[Path] = set()
    files = [d / "nodes.yaml"]
    if d.is_dir():
        files += sorted(p / "nodes.yaml" for p in d.iterdir()
                        if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "nodes.yaml").is_file())
    for f in files:
        if not f.is_file():
            continue
        try:
            import yaml

            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            names = doc.get("node_dirs") if isinstance(doc, dict) else None
            if not isinstance(names, list):
                raise TypeError("node_dirs は list")
            for name in names:
                if not isinstance(name, str) or not name or PurePosixPath(name).is_absolute() or "\\" in name:
                    raise TypeError(f"node_dirs の項目は相対パスの文字列: {name!r}")
                p = (f.parent / name).resolve()
                if p in seen:
                    continue
                seen.add(p)
                out.append(p)
        except Exception as e:  # noqa: BLE001  宣言の失敗で部品庫を止めない
            log.error("node_dirs の宣言 %s を読めない: %s", f, type(e).__name__)
    return out


def subloop_registry(root: str | Path, plugin_dir: str | Path, *, extra_dirs: list | None = None) -> Registry:
    """サブループが使う部品庫: 共通の `plugins/nodes`、サブループ自身の `nodes/`、宣言された `node_dirs`（D-6.3、S-7.4）。"""
    dirs: list[str | Path] = [Path(root) / "plugins" / "nodes", Path(plugin_dir) / "nodes", *declared_node_dirs(plugin_dir)]
    if extra_dirs:
        dirs += extra_dirs
    return load_registry(*dirs)
