"""索引の確認（F-18.6 の B8: archive/ の秘密も含めること）。secrets_dir/archive/ の全ファイルの値が索引にあるかを調べる。値は出さない。

  python3 -P -m hostwatch.indexverify --secrets-dir DIR --index FILE
終了コード: 0 全部ある（または archive/ が無い）/ 1 足りない（件数だけ出す）/ 2 索引が読めない
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from axcommon.redact_index import collect_values, read_salt, value_hash


def missing_count(secrets_dir: Path, index: Path) -> int | None:
    salt = read_salt(index)
    if salt is None:
        return None
    try:
        known = set(index.read_text(encoding="utf-8").split("\n")[1:])
    except OSError:
        return None
    arch = secrets_dir / "archive"
    if not arch.is_dir():
        return 0
    return len({h for h in (value_hash(salt, v) for v in collect_values(arch)) if h not in known})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hostwatch.indexverify")
    ap.add_argument("--secrets-dir", required=True)
    ap.add_argument("--index", required=True)
    a = ap.parse_args(argv)
    n = missing_count(Path(a.secrets_dir), Path(a.index))
    if n is None:
        print("ax-redact-index: 索引が読めない", file=sys.stderr)
        return 2
    if n:
        print(f"ax-redact-index: archive/ の値のうち {n} 件が索引に無い", file=sys.stderr)
        return 1
    print("ax-redact-index: archive/ の値はすべて索引にある")
    return 0


if __name__ == "__main__":
    sys.exit(main())
