"""Exercise manager polling through the real timeline HTTP server."""
import socket
import threading
import time
from types import SimpleNamespace

import uvicorn

from axcommon.client import TimelineClient, urllib_transport
from axcommon.redact import Redactor
from core.manager.config import ManagerConfig
from core.manager.cycle import Manager
from core.manager.store import Store
from core.timeline import app as timeline_app

_REAL_CONNECT = socket.socket.connect


def test_manager_longpoll_and_connection_failure(tmp_path, monkeypatch):
    cfg = timeline_app.Config(db=str(tmp_path / "timeline.sqlite"), archive_dir=str(tmp_path / "archive"),
                              export_interval_s=0, hot_days=30, longpoll_s=0.35)
    app, _, service = timeline_app.build(cfg, redactor=Redactor(), run_heartbeat_loop=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    endpoint = sock.getsockname()
    address = f"http://127.0.0.1:{endpoint[1]}"

    def connect_loopback_only(sock, target):
        if target != endpoint:
            raise RuntimeError("unexpected network destination")
        return _REAL_CONNECT(sock, target)

    monkeypatch.setattr(socket.socket, "connect", connect_loopback_only)
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append(timeout)
        return urllib_transport(method, url, headers, body, timeout)

    store = Store()
    client = TimelineClient(address, timeout=0.1, transport=transport)
    manager = Manager(SimpleNamespace(timeline=client), store, None, ManagerConfig(), None,
                      outbox_dir=tmp_path / "outbox")
    try:
        assert manager.fetch() == 0 and manager.timeline_ok
        assert calls[-1] == 0.1
        assert TimelineClient(address, transport=transport).get_events()["events"] is not None
        assert calls[-1] == 10.0
        start = time.monotonic()
        assert manager.fetch(wait=0.35) == 0 and manager.timeline_ok
        assert time.monotonic() - start >= 0.3
        assert calls[-1] == 10.35
        start = time.monotonic()
        assert manager.fetch(wait=30) == 0 and manager.timeline_ok
        assert time.monotonic() - start >= 0.3
        assert calls[-1] == 40.0
        assert client.timeout == 0.1
        assert manager.fetch() == 0 and manager.timeline_ok
        assert calls[-1] == 0.1
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        service.store.close()
    assert not thread.is_alive()
    try:
        assert manager.fetch(wait=0.35) == 0
        assert not manager.timeline_ok
    finally:
        store.close()
