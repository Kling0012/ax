"""python /ax/core/memory-connector/main.py  — 8080（ax-core）と 8081（ax-work）を同じプロセスで待ち受ける。

結び付け先は core.timeline.netbind と同じ（8080 は ax-core のアドレスと 127.0.0.1、8081 は ax-work のアドレスだけ。F-18.4）。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))  # /ax（axcommon と core.timeline.netbind）


async def _serve() -> int:
    import uvicorn

    from core.timeline import netbind
    from memconn import app as app_mod

    core, work, _svc = app_mod.build()
    core_hosts, work_hosts = netbind.resolve_binds()
    servers = [uvicorn.Server(uvicorn.Config(core, log_level="info")).serve(sockets=netbind.bind_sockets(core_hosts, 8080))]
    if work_hosts:
        servers.append(uvicorn.Server(uvicorn.Config(work, log_level="info")).serve(sockets=netbind.bind_sockets(work_hosts, 8081)))
    else:
        logging.getLogger("memory-connector").warning("ax-work 側のアドレスが見つからない。8081 は開かない")
    await asyncio.gather(*servers)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(_serve())


if __name__ == "__main__":
    sys.exit(main())
