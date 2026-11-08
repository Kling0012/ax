"""ax-wp-probe-prefix（S-8.8、S-8.12 の 14）。構築者が B6 の受け入れで、worker-pool の内部コマンドとして即時に計測を走らせる。

使い方（コンテナ内で docker compose exec により呼ぶ）:
  docker compose exec worker-pool python -m plugins.resource.worker-pool.probe_cli [--model ID]
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ax-wp-probe-prefix")
    ap.add_argument("--model", default=None, help="計測するモデル ID（なければ unknown の全モデル）")
    ap.add_argument("--store", default="/data/resources.sqlite")
    args = ap.parse_args(argv)
    probe = importlib.import_module("plugins.resource.worker-pool.probe")
    from core.resources.store import Store
    store = Store(args.store)
    models = store.list(limit=2000)
    targets = [args.model] if args.model else probe.is_unknown_models(models)
    out = {"probe_prefix_id": probe.probe_prefix_id(), "models": targets}
    print(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
