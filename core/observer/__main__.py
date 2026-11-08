"""python -m core.observer — 8080 に /healthz を開き、observe_s ごとに検査を回す。

結び付け先は core_only_binds（D-14。ax-core のアドレスと 127.0.0.1 だけ）。環境変数 AX_OBSERVER_BIND
（カンマ区切り）で上書きできる。0.0.0.0 では待ち受けない（compose に ports は無い。ax-core の網の中だけ）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from .app import build_observer, create_observer_app


def resolve_binds(environ: dict | None = None, addresses: list[str] | None = None) -> list[str]:
    from core.timeline import netbind

    return netbind.core_only_binds("AX_OBSERVER_BIND", environ, addresses)


async def _serve() -> int:
    import uvicorn

    from core.timeline import netbind

    obs = build_observer()
    app = create_observer_app(obs)
    port = int(os.environ.get("AX_HEALTH_PORT", "8080"))
    hosts = resolve_binds()
    sockets = netbind.bind_sockets(hosts, port)
    if len(hosts) <= 1:
        logging.getLogger("observer").warning("ax-core のアドレスが見つからない。127.0.0.1 だけに結び付ける")
    servers = [uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on")).serve(sockets=[s]) for s in sockets]
    await asyncio.gather(*servers)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(_serve())


if __name__ == "__main__":
    sys.exit(main())
