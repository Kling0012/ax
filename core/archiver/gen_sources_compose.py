#!/usr/bin/env python3
"""archive-sources.yaml から、archiver の /src/<name> の bind（ro）を足す compose の断片を作る（F-20）。

compose は YAML の一覧を回せないため、退避元の bind は一覧から生成する。出力は標準出力か --out（一時ファイルから置き換えで書く）。
存在しないパスは出力に入れない（F-10: 無い物は登録しない）。`~` は --home（既定 $HOME）で展開する。
host/ax-up.sh が起動のたびにこの断片を `${ax_state_dir}/deploy/archive-sources.compose.yaml` へ再生成し、`-f` に足す。
退避元の一覧（`--sources`）は profile のディレクトリの `archive-sources.yaml`（AX_ARCHIVE_SOURCES_FILE で変えられる）。使い方:
  python3 -m core.archiver.gen_sources_compose --sources ~/.config/ax/profile/archive-sources.yaml --out ~/.local/state/ax/deploy/archive-sources.compose.yaml
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from .config import ConfigError, parse_sources


def build(doc, *, home: str, exists=os.path.exists) -> dict:
    vols = []
    skipped = []
    for s in parse_sources(doc):
        p = s.path.replace("~", home, 1) if s.path.startswith("~") else s.path
        if not exists(p):
            skipped.append(s.name)
            continue
        vols.append({"type": "bind", "source": p, "target": f"/src/{s.name}", "read_only": True})
    return {"services": {"archiver": {"volumes": vols}}, "_skipped": skipped}


def render(out: dict) -> str:
    import yaml

    body = {"services": out["services"]}
    head = "# archive-sources.yaml から生成した退避元の bind（ro）。手で直さない。生成: python3 -m core.archiver.gen_sources_compose\n"
    return head + yaml.safe_dump(body, allow_unicode=True, sort_keys=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gen_sources_compose")
    ap.add_argument("--sources", required=True)
    ap.add_argument("--out")
    ap.add_argument("--home", default=os.environ.get("HOME", ""))
    a = ap.parse_args(argv)
    import yaml

    try:
        doc = yaml.safe_load(Path(a.sources).read_text(encoding="utf-8"))
        out = build(doc, home=a.home)
    except (OSError, ConfigError, yaml.YAMLError) as e:
        print(f"gen_sources_compose: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    text = render(out)
    if out["_skipped"]:
        print("gen_sources_compose: 存在しないので入れない: " + ", ".join(out["_skipped"]), file=sys.stderr)
    if a.out:
        dest = Path(a.out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", dir=str(dest.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, dest)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
