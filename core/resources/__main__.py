"""python -m core.resources — 8080（ax-core）と 8081（ax-work）を同じプロセスで待ち受ける。

結び付け先は netbind.resolve_binds。ax-work 側のアドレスが見つからないときは 8081 を開かない（8080 だけで動く）。
"""
from __future__ import annotations

import asyncio
import logging
import sys

from . import app as app_mod, netbind


async def _serve() -> int:
    import uvicorn
    from starlette.applications import Starlette
    from .api import build_routes

    core, work, svc = app_mod.build()
    core_hosts, work_hosts = netbind.resolve_binds()
    servers = []
    cfg_core = uvicorn.Config(core, log_level="info", lifespan="on")
    servers.append(uvicorn.Server(cfg_core).serve(sockets=netbind.bind_sockets(core_hosts, 8080)))
    if work_hosts:
        cfg_work = uvicorn.Config(work, log_level="info", lifespan="on")
        servers.append(uvicorn.Server(cfg_work).serve(sockets=netbind.bind_sockets(work_hosts, 8081)))
    else:
        logging.getLogger("resources").warning("ax-work 側のアドレスが見つからない。8081 は開かない")
    review_hosts = netbind.resolve_review_binds()
    if review_hosts:
        review = Starlette(routes=build_routes(svc, work_side=False, review_side=True))
        cfg_review = uvicorn.Config(review, log_level="info", lifespan="off")
        servers.append(uvicorn.Server(cfg_review).serve(sockets=netbind.bind_sockets(review_hosts, 8080)))
    await asyncio.gather(*servers)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(_serve())


if __name__ == "__main__":
    sys.exit(main())
