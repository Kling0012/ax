"""python3 -P -m hostwatch {cycle|deploy} — host/ax-hostwatch.sh と host/ax-deploy.sh の本体。"""
from __future__ import annotations

import sys

from . import config


def main(argv: list[str] | None = None) -> int:
    a = sys.argv[1:] if argv is None else argv
    if not a or a[0] not in ("cycle", "deploy"):
        print("usage: hostwatch {cycle|deploy}", file=sys.stderr)
        return 2
    try:
        cfg = config.load()
    except Exception as e:  # noqa: BLE001  profile が読めない: 何もしない（fail-soft。journal に理由だけ）
        print(f"ax-hostwatch: profile を読めない（{type(e).__name__}）。この周期は何もしない", file=sys.stderr)
        return 1
    if a[0] == "cycle":
        from .watch import run_cycle

        run_cycle(cfg)
        return 0
    from .deploy import main as deploy_main

    return deploy_main(cfg)


if __name__ == "__main__":
    sys.exit(main())
