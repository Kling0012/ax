"""Run only when explicitly invoked by the HQ container command."""
from __future__ import annotations

import os


def main():
    from axcommon.client import TimelineClient, service_url
    from axcommon.events import EventSink
    from .app import create_app
    from .runtime import serve
    from .settings import Settings

    settings = Settings.load()
    sink = EventSink(TimelineClient(service_url("timeline")), "hq", os.environ["AX_HOST"], "/run/hq/outbox")
    app = create_app(settings=settings, sink=sink)
    from .runtime import start_receipt_listener
    hub = getattr(app.state, "notify", None)
    if hub is not None:
        from .ext.b10a import receipt_app
        start_receipt_listener(receipt_app(hub, settings))
    serve(app, settings.socket_path, expected_uid=int(os.environ["AX_HQ_UID"]))


if __name__ == "__main__":
    main()
