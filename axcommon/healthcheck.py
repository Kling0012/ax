"""コンテナの healthcheck 用の CLI（docker/base.yaml）。標準ライブラリだけ。

AX_HEALTH_PORT（既定 8080）の /healthz に GET し、200 で {"ok": true} なら終了コード 0。
"""
import json
import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("AX_HEALTH_PORT", "8080")
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310
            body = json.loads(r.read().decode("utf-8"))
        return 0 if r.status == 200 and body.get("ok") is True else 1
    except Exception:  # noqa: BLE001
        return 1


if __name__ == "__main__":
    sys.exit(main())
