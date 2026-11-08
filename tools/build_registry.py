#!/usr/bin/env python3
"""plugins/registry.yaml を plugins/registry.d/*.yaml の断片から作る（S-4.1、PLAN.json の conventions）。

各束は plugins/registry.d/<束の ID>.yaml に自分のプラグインを書く（共有ファイルの同時編集を避ける）。
断片の形は schemas/registry-fragment.schema.json:  plugins: [{id, kind, path}]  （path は manifest.yaml の置き場）。
このツールが断片を束ね、ID の重複を拒否し、id 順に並べて registry.yaml を作る。制御 API は起動時に registry.yaml を読み、
載っているプラグインだけを登録する。新しいサブループを足せるのは入口の行き先 subloop.create と core.change だけ（S-4.1）。

使い方:
  python3 tools/build_registry.py --write   registry.yaml を作り直す
  python3 tools/build_registry.py --check   registry.yaml が断片と一致するか調べる（CI 用。終了コード 0/1）
  オプション: --root DIR（リポジトリの根。既定はこのファイルの親の親）
manifest.yaml が実在するとき、その id と kind が断片と一致しなければ不合格。実在しない path は警告（束の完成前の断片を許す）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HEADER = ("# プラグインの登録簿（S-4.1）。生成物。手で直さない。\n"
          "# 直し方: plugins/registry.d/<束の ID>.yaml を直し、python3 tools/build_registry.py --write\n")


def build(root: Path) -> tuple[str, list[str], list[str]]:
    import yaml

    sys.path.insert(0, str(root))
    from axcommon import schemas

    errors: list[str] = []
    warns: list[str] = []
    seen: dict[str, str] = {}
    entries: list[dict] = []
    frag_dir = root / "plugins" / "registry.d"
    for f in sorted(frag_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            errors.append(f"{f.name}: YAML として読めない")
            continue
        errs = schemas.errors("registry-fragment", doc)
        if errs:
            errors += [f"{f.name}: {e}" for e in errs]
            continue
        for p in doc["plugins"]:
            if p["id"] in seen:
                errors.append(f"{f.name}: プラグイン {p['id']} が {seen[p['id']]} と重複")
                continue
            seen[p["id"]] = f.name
            mpath = root / p["path"]
            if mpath.is_file():
                try:
                    m = yaml.safe_load(mpath.read_text(encoding="utf-8")) or {}
                except yaml.YAMLError:
                    errors.append(f"{p['path']}: YAML として読めない")
                    m = {}
                if m and (m.get("id") != p["id"] or m.get("kind") != p["kind"]):
                    errors.append(f"{p['path']}: manifest の id/kind（{m.get('id')}/{m.get('kind')}）が断片（{p['id']}/{p['kind']}）と違う")
            else:
                warns.append(f"{p['path']}: manifest がまだ無い（{f.name}）")
            entries.append({"id": p["id"], "kind": p["kind"], "path": p["path"], "source": f.name})
    entries.sort(key=lambda e: e["id"])
    doc = {"schema_version": 1, "plugins": entries}
    body = HEADER + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=200)
    errors += [f"registry: {e}" for e in schemas.errors("registry", doc)]
    return body, errors, warns


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    a = ap.parse_args(argv)
    root = Path(a.root)
    body, errors, warns = build(root)
    for w in warns:
        print("警告:", w, file=sys.stderr)
    if errors:
        for e in errors:
            print("NG:", e, file=sys.stderr)
        return 1
    target = root / "plugins" / "registry.yaml"
    if a.check:
        cur = target.read_text(encoding="utf-8") if target.exists() else ""
        if cur != body:
            print("NG: plugins/registry.yaml が断片と一致しない。python3 tools/build_registry.py --write", file=sys.stderr)
            return 1
        print("OK: registry.yaml は断片と一致")
        return 0
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)
    print(f"OK: {target} を書いた")
    return 0


if __name__ == "__main__":
    sys.exit(main())
