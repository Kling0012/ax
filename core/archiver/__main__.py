"""python -m core.archiver  — healthz（8080。ax-core の網の中だけ）と巡回。

結び付け先は core_only_binds（D-14。ax-core のアドレスと 127.0.0.1 だけ）。環境変数 AX_ARCHIVER_BIND
（カンマ区切り）で上書きできる。0.0.0.0 では待ち受けない（compose に ports は無い）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from . import app as app_mod


def resolve_binds(environ: dict | None = None, addresses: list[str] | None = None) -> list[str]:
    from core.timeline import netbind

    return netbind.core_only_binds("AX_ARCHIVER_BIND", environ, addresses)


async def _serve() -> int:
    import uvicorn

    from core.timeline import netbind

    archiver = app_mod.build_archiver()
    app = app_mod.build_app(archiver, interval_s=float(os.environ.get("AX_ARCHIVE_INTERVAL_S", 3600)),
                            first_delay_s=float(os.environ.get("AX_ARCHIVE_FIRST_DELAY_S", 60)))
    port = int(os.environ.get("AX_HEALTH_PORT", "8080"))
    hosts = resolve_binds()
    sockets = netbind.bind_sockets(hosts, port)
    if len(hosts) <= 1:
        logging.getLogger("archiver").warning("ax-core のアドレスが見つからない。127.0.0.1 だけに結び付ける")
    servers = [uvicorn.Server(uvicorn.Config(app, log_level="info", lifespan="on")).serve(sockets=[s]) for s in sockets]
    await asyncio.gather(*servers)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(_serve())


if __name__ == "__main__":
    sys.exit(main())
