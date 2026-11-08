"""intake の起動（compose の command: python -m core.intake）。

- commander ソケット（F-9.2 の (3)）: `/run/commander/commander.sock`。起動のたびに残っていれば外し、
  `umask 077` で bind し、権限 0600 にして待ち受ける。ディレクトリの所有者が自分でない、または
  group・other に権限があるときは待ち受けず `health.alert` を書く（他の部品は止めない）。
- healthz 用の TCP（AX_HEALTH_PORT、127.0.0.1 だけ。指示の口は載せない。S-15.5）。
- 後続の束の差し込み口（app.state.intake.extra_listener_factories）の待ち受けも順に始める。
  1 つの失敗はその待ち受けだけを止める。
- 他の部品の起動は待たない（D-5。落ちている部品は行き先の応答で `unavailable` になる）。
"""
from __future__ import annotations

import logging
import os
import sys

from . import __version__, app as app_mod
from .httpsock import Dispatcher, HttpListener, run_until_signal

log = logging.getLogger("axintake.main")


def main() -> int:
    logging.basicConfig(level=os.environ.get("AX_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from axcommon.client import TimelineClient
    from axcommon.events import EventSink

    st_holder: dict = {}

    sink = EventSink(TimelineClient(app_mod.service_url("timeline"), transport=None), "intake",
                     app_mod.host_name(), app_mod.outbox_dir())
    from .audit import AccessLog

    audit = AccessLog(app_mod.commander_dir() / "access.log")
    from .idempotency import IdempotencyStore

    from axcommon.defaults import Defaults

    idem = IdempotencyStore(app_mod.idem_path(),
                            retain_h=float(Defaults.load().get("idempotency_retain_h")))
    application = app_mod.build_app(sink=sink, idem=idem, audit=audit, transport=None, version=__version__)
    st = application.state.intake
    st_holder["state"] = st

    health_app = app_mod.build_health_app(st, version=__version__)
    dispatcher = Dispatcher(application, on_auth_failed=_auth_failed(st, audit))
    dispatcher.start()
    health_dispatcher = Dispatcher(health_app)
    health_dispatcher.start()

    listeners: list[HttpListener] = []

    # commander ソケット（F-9.2）
    commander = HttpListener(name="commander", dispatcher=dispatcher,
                             unix_path=app_mod.commander_dir() / "commander.sock",
                             via_name="local", on_auth_failed=_auth_failed(st, audit))
    _start(commander, st, sink, listeners)

    # healthz 用の TCP（127.0.0.1 だけ）
    port = int(os.environ.get("AX_HEALTH_PORT") or "8080")
    health = HttpListener(name="health", dispatcher=health_dispatcher, host="127.0.0.1", port=port)
    _start(health, st, sink, listeners, required=False)

    # 後続の束の待ち受け（seat.sock、hq ソケット、gate 用の TCP。b12b1 以降）
    for i, factory in enumerate(list(st.extra_listener_factories)):
        try:
            made = factory(dispatcher)
        except Exception:  # noqa: BLE001  差し込み 1 つの失敗で入口を止めない
            log.exception("追加の待ち受け %d を作れない", i)
            continue
        for l in made if isinstance(made, (list, tuple)) else [made]:
            _start(l, st, sink, listeners, required=False)

    run_until_signal(listeners, dispatcher)
    health_dispatcher.shutdown()
    st.executor.shutdown(wait=False)
    return 0


def _auth_failed(st, audit):
    def hook(via: str, reason: str) -> None:
        from axcommon import timeutil

        app_mod._emit(st, "auth.failed", "intake", {"via": via, "reason": reason})
        if audit is not None:
            audit.write(ts=timeutil.now_ts(), via=via, principal=None, method="CONNECT", path="-", status=403)

    return hook


def _start(listener: HttpListener, st, sink, listeners: list, *, required: bool = True) -> None:
    ok = listener.start()
    st.listener_status[listener.name] = {"listening": ok, "refusal": listener.refusal}
    listeners.append(listener)
    if not ok:
        # 待ち受けられない。health.alert を書いて、他の部品も他の待ち受けも止めない
        app_mod.health_alert(st, "intake_listen_failed", f"{listener.name}:{listener.refusal}", severity="error")
        log.error("待ち受け %s が始められない（%s）", listener.name, listener.refusal)


if __name__ == "__main__":
    sys.exit(main())
