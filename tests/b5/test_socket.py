"""commander ソケット（F-9.2、D-5）と httpsock（ASGI の入れ物）の試験。

socket.connect は試験で禁止されている（tests/conftest.py）ため、待ち受けとソケットの準備は
実物の Unix ソケットで、要求の処理は socketpair（接続済みのペア）で確かめる。
"""
from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time

import pytest

from core.intake import httpsock
from core.intake.app import build_health_app, health_extra
from core.intake.httpsock import Dispatcher, HttpListener, connection_loop, peercred

from .helpers import client_for, make_app, simple_dest


def _read_http(sock) -> bytes:
    sock.settimeout(5)
    chunks = []
    while True:
        try:
            b = sock.recv(65536)
        except OSError:
            break
        if not b:
            break
        chunks.append(b)
        if b"\r\n\r\n" in b"".join(chunks):
            head, _, rest = b"".join(chunks).partition(b"\r\n\r\n")
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    n = int(line.split(b":", 1)[1])
                    if len(rest) >= n:
                        return b"".join(chunks)
    return b"".join(chunks)


def test_listener_binds_unix_socket_with_0600_and_removes_stale(tmp_path):
    path = tmp_path / "commander.sock"
    path.write_text("", encoding="utf-8")  # 残っていた古いファイル（自分の物として外す）
    d = Dispatcher(_dummy_app())
    d.start()
    try:
        l = HttpListener(name="t", dispatcher=d, unix_path=path, via_name=None)
        assert l.start() is True
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600  # umask 077 で bind して 0600（F-9.2）
        l.stop()
        assert not path.exists()  # 止まるときは自分のソケットのファイルだけ外す
    finally:
        d.shutdown()


def test_listener_refuses_when_directory_permissions_are_wrong(tmp_path):
    d = Dispatcher(_dummy_app())
    d.start()
    try:
        bad = tmp_path / "bad"
        bad.mkdir()
        os.chmod(bad, 0o707)  # group・other に権限がある（mkdir の mode は umask で削られるので後から付ける）
        l = HttpListener(name="t", dispatcher=d, unix_path=bad / "commander.sock", via_name=None)
        assert l.start() is False
        assert l.refusal == "dir_owner_or_mode"
        assert not (bad / "commander.sock").exists()

        missing = tmp_path / "missing"
        l2 = HttpListener(name="t2", dispatcher=d, unix_path=missing / "x.sock", via_name=None)
        assert l2.start() is False and l2.refusal and l2.refusal.startswith("dir_missing")
    finally:
        d.shutdown()


def test_listener_refuses_when_path_is_not_a_socket(tmp_path):
    d = Dispatcher(_dummy_app())
    d.start()
    try:
        notsock = tmp_path / "plain"
        notsock.mkdir(mode=0o700)
        (notsock / "x.sock").mkdir()  # ディレクトリは外さない
        l = HttpListener(name="t", dispatcher=d, unix_path=notsock / "x.sock", via_name=None)
        assert l.start() is False and l.refusal == "path_not_socket"
    finally:
        d.shutdown()


def _dummy_app():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def ping(request):
        return JSONResponse({"ok": True, "path": request.url.path})

    return Starlette(routes=[Route("/ping", ping, methods=["GET"])])


def test_healthz_over_socketpair_without_ident(fake_transport, tmp_path):
    """受け口の資格が無い待ち受けでも、healthz は通る（D-5: 待ち受けが済むと healthz が ok）。"""
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    app = make_app(tmp_path, fake_transport)
    d = Dispatcher(app)
    d.start()
    a, b = socket.socketpair()
    try:
        t = threading.Thread(target=connection_loop, kwargs={
            "conn": b, "dispatcher": d, "scheme": "http", "server": ("unix", "t"), "via_name": None}, daemon=True)
        t.start()
        a.sendall(b"GET /healthz HTTP/1.1\r\nHost: commander\r\nConnection: close\r\n\r\n")
        raw = _read_http(a)
        assert b"200" in raw.split(b"\r\n")[0]
        body = json.loads(raw.partition(b"\r\n\r\n")[2])
        assert body["ok"] is True and body["component"] == "intake"
    finally:
        a.close()
        b.close()
        d.shutdown()


def test_instruction_over_socketpair_with_local_uid(fake_transport, tmp_path, monkeypatch):
    """commander ソケットを通る指示（F-9.2）。接続元 uid の照合を通った接続だけが指示を出せる。"""
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    fake_transport.add("POST", "/v1/echo", json={"echoed": True})
    audit_path = tmp_path / "access.log"
    app = make_app(tmp_path, fake_transport, audit_path=audit_path)
    monkeypatch.setenv("AX_UID", str(os.geteuid()))
    monkeypatch.setenv("AX_LOCAL_PRINCIPAL", "configured-operator")
    d = Dispatcher(app)
    d.start()
    a, b = socket.socketpair()
    try:
        creds = peercred(b)
        assert creds is not None and creds[1] == os.geteuid()
        t = threading.Thread(target=connection_loop, kwargs={
            "conn": b, "dispatcher": d, "scheme": "http", "server": ("unix", "t"), "via_name": "local"}, daemon=True)
        t.start()
        body = json.dumps({"from": "commander", "destination": "demo.echo",
                           "payload": {"ok": True, "component": "d", "version": "1"}})
        a.sendall(("POST /v1/instructions HTTP/1.1\r\nHost: commander\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n" + body).encode())
        raw = _read_http(a)
        head = raw.split(b"\r\n")[0]
        assert b"200" in head
        out = json.loads(raw.partition(b"\r\n\r\n")[2])
        assert out["ok"] is True and out["destination"] == "demo.echo" and out["result"] == {"echoed": True}
        assert json.loads(audit_path.read_text().splitlines()[-1])["principal"] == "configured-operator"
    finally:
        a.close()
        b.close()
        d.shutdown()


def test_uid_mismatch_on_local_socket_is_refused(fake_transport, tmp_path, monkeypatch):
    """uid が司令塔のユーザーでない接続は、本文が何であれ 403（auth.failed。S-6.8、T-6 の一部）。"""
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    app = make_app(tmp_path, fake_transport)
    monkeypatch.setenv("AX_UID", str(os.geteuid() + 12345))  # 違う uid を期待させる
    d = Dispatcher(app, on_auth_failed=lambda via, reason: None)
    d.start()
    a, b = socket.socketpair()
    try:
        t = threading.Thread(target=connection_loop, kwargs={
            "conn": b, "dispatcher": d, "scheme": "http", "server": ("unix", "t"), "via_name": "local"}, daemon=True)
        t.start()
        body = json.dumps({"from": "hq", "destination": "control.command", "payload": {}})
        raw = b""
        try:
            a.sendall(("POST /v1/instructions HTTP/1.1\r\nHost: commander\r\n"
                       f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n" + body).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass  # 拒否が先に返って接続が閉じることがある。応答を読むだけでよい
        try:
            raw = _read_http(a)
        except OSError:
            pass
        assert b"403" in raw.split(b"\r\n")[0]
        out = json.loads(raw.partition(b"\r\n\r\n")[2])
        assert out["error"]["code"] == "auth.failed"
    finally:
        a.close()
        b.close()
        d.shutdown()


def test_local_ident_and_hq_ident_gate():
    from core.intake.vias import hq_ident, local_ident, local_principal

    ident = local_ident(os.geteuid(), allowed_uid=os.geteuid())
    assert ident and ident.via == "local" and ident.from_ == "commander"
    assert ident.principal == local_principal() == "local-operator"
    assert ident.max_tier == "operate"  # local の段は operate に固定（S-6.8）
    assert local_ident(os.geteuid() + 1, allowed_uid=os.geteuid()) is None
    assert hq_ident(1001, allowed_uid=None) is None  # AX_HQ_UID が無いときは hq 口を開けない
    hq = hq_ident(1001, allowed_uid=1001)
    assert hq.principal == "user" and hq.max_tier == "irreversible"


def test_health_extra_turns_ok_false_when_a_listener_is_down(fake_transport, tmp_path):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    app = make_app(tmp_path, fake_transport)
    st = app.state.intake
    extra = health_extra(st)
    assert "listeners" not in extra()  # 待ち受けがまだ無い
    st.listener_status["commander"] = {"listening": True, "refusal": None}
    assert "ok" not in extra()  # 全部待ち受けられていれば ok は true のまま
    st.listener_status["commander"] = {"listening": False, "refusal": "dir_owner_or_mode"}
    assert extra()["ok"] is False  # 待ち受けが済むと healthz が ok になる（F-9.2）。逆は ok でない
    from starlette.testclient import TestClient

    with TestClient(build_health_app(st)) as c:
        r = c.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] is False


def test_bad_request_line_gets_400(tmp_path):
    d = Dispatcher(_dummy_app())
    d.start()
    a, b = socket.socketpair()
    try:
        t = threading.Thread(target=connection_loop, kwargs={
            "conn": b, "dispatcher": d, "scheme": "http", "server": ("unix", "t"), "via_name": None}, daemon=True)
        t.start()
        a.sendall(b"BROKEN LINE\r\n\r\n")
        raw = _read_http(a)
        assert b"400" in raw.split(b"\r\n")[0]
    finally:
        a.close()
        b.close()
        d.shutdown()
