"""Exercise seat and HQ listener startup with real local UNIX sockets."""
import errno
import os
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from core.intake.seat.listener import SeatListener

_ORIGINAL_CONNECT = socket.socket.connect


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def local_only(sock, address):
        if sock.family != socket.AF_UNIX:
            raise RuntimeError("only local UNIX sockets are allowed")
        return _ORIGINAL_CONNECT(sock, address)
    monkeypatch.setattr(socket.socket, "connect", local_only)


def make_listener(directory, name, via):
    directory.mkdir()
    directory.chmod(0o2710)
    service = SimpleNamespace(
        store=SimpleNamespace(archive=directory.parent / "other-mount", _alert=lambda *_: None),
        authenticate_connection=lambda *_: None,
    )
    return SeatListener(name=name, dispatcher=object(), unix_path=directory / "socket", via_name=via,
                        service=service, group_id=os.getegid())


def request(socket_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(socket_path))
        return client.recv(4096).split(b"\r\n", 1)[0]


@pytest.mark.parametrize("via", ["ssh", "hq"])
def test_stale_socket_archived_inside_socket_mount(tmp_path, monkeypatch, via):
    listener = make_listener(tmp_path / "s", "test", via)
    path = Path(listener.unix_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(path))
    old = path.lstat().st_ino
    original_rename = os.rename
    calls = []

    def reject_cross_mount(src, dst, *args, **kwargs):
        calls.append((Path(src), Path(dst)))
        if Path(dst).is_relative_to(listener.service.store.archive):
            raise OSError(errno.EXDEV, "cross-device link")
        return original_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", reject_cross_mount)
    assert listener.start(), listener.refusal
    try:
        archived = list((path.parent / "archive").glob("*/socket"))
        assert len(archived) == 1 and archived[0].lstat().st_ino == old
        assert calls[0][1] == archived[0]
        assert request(path).startswith(b"HTTP/1.1 403")
    finally:
        listener.stop()
    assert len(list((path.parent / "archive").glob("*/socket"))) == 2


def test_live_listener_and_other_listener_survive_archive_failure(tmp_path, monkeypatch):
    first = make_listener(tmp_path / "a", "seat", "ssh")
    other = make_listener(tmp_path / "b", "hq", "hq")
    assert first.start() and other.start()
    try:
        path = Path(first.unix_path)
        original = path.lstat().st_ino
        contender = SeatListener(name="seat-duplicate", dispatcher=object(), unix_path=path, via_name="ssh",
                                 service=first.service, group_id=os.getegid())
        assert not contender.start()
        assert contender.refusal == "socket_in_use"
        assert path.lstat().st_ino == original
        assert request(path).startswith(b"HTTP/1.1 403")

        broken = make_listener(tmp_path / "c", "broken", "ssh")
        broken_path = Path(broken.unix_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(broken_path))
        def deny_archive(*args, **kwargs):
            raise PermissionError("test")
        monkeypatch.setattr(broken, "_archive_socket", deny_archive)
        assert not broken.start()
        assert broken.refusal == "bind_failed:PermissionError"
        assert broken_path.is_socket()
        assert request(Path(other.unix_path)).startswith(b"HTTP/1.1 403")
    finally:
        first.stop()
        other.stop()
