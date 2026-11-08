"""tailnet の入口 gate（S-6.14、D-5.3、D-14）。

結び付け先は core_only_binds（ax-core のアドレスと 127.0.0.1 だけ）。環境変数 AX_GATE_BIND
（カンマ区切り）で上書きできる。0.0.0.0 では待ち受けない（compose の ports は
${AX_BIND_ADDR}:${AX_GATE_PORT}:8080 の 1 行だけで、host_ip は tailnet のアドレスか 127.0.0.1）。
docker の ports 転送はコンテナ内の ax-core のアドレスに届くので、ax-core のアドレスへの結び付けが要る。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from .app import build_from_environment


def resolve_binds(environ: dict | None = None, addresses: list[str] | None = None) -> list[str]:
    from core.timeline import netbind

    return netbind.core_only_binds("AX_GATE_BIND", environ, addresses)


async def _serve() -> int:
    import uvicorn

    from core.timeline import netbind

    app = build_from_environment()
    port = int(os.environ.get("AX_HEALTH_PORT", "8080"))
    hosts = resolve_binds()
    if len(hosts) <= 1:
        # ax-core のアドレスが無いと ports 転送に届かない。起動を止めずに記録する（fail-soft）。
        logging.getLogger("gate").warning("ax-core のアドレスが見つからない。127.0.0.1 だけに結び付ける")
    sockets = netbind.bind_sockets(hosts, port)
    servers = [uvicorn.Server(uvicorn.Config(app, log_level="info", lifespan="on", access_log=False)).serve(sockets=[s])
               for s in sockets]
    await asyncio.gather(*servers)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(_serve())


if __name__ == "__main__":
    raise SystemExit(main())
