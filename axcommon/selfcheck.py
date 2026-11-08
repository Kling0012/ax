"""axcommon の自己点検（compose.d/b0.yaml のサービス `axcommon-selfcheck`。プロファイル b0）。

イメージの中で、(1) 必要な AX_ の環境変数がある、(2) TMPDIR が書ける、(3) schemas/ と config/ が読める、を確かめて JSON を 1 行出す。
終了コード 0 なら全部通った。値（パスやアドレス）は出さない。名前と結果だけ。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile


def run(environ=None) -> dict:
    env = os.environ if environ is None else environ
    from . import __version__

    res: dict = {"component": "axcommon-selfcheck", "version": __version__, "checks": {}}
    ok = True
    for var in ("TMPDIR", "AX_STATE_DIR"):
        present = bool(env.get(var))
        res["checks"]["env:" + var] = present
        ok &= present
    try:
        with tempfile.NamedTemporaryFile(dir=env.get("TMPDIR") or None):
            res["checks"]["tmp_writable"] = True
    except OSError:
        res["checks"]["tmp_writable"] = False
        ok = False
    try:
        from . import schemas
        from .defaults import Defaults
        from .errors import ErrorRules

        bad = schemas.check_all()
        res["checks"]["schemas"] = not bad
        Defaults.load()
        ErrorRules.load()
        res["checks"]["config"] = True
        ok &= not bad
    except Exception as e:  # noqa: BLE001
        res["checks"]["schemas_or_config"] = type(e).__name__
        ok = False
    res["ok"] = ok
    return res


def main() -> int:
    r = run()
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
