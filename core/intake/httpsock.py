"""Unix ソケットと TCP で ASGI アプリを受けられる小さな HTTP サーバ（F-9.2、D-5）。

uvicorn を使わない理由: commander ソケットは接続ごとに `SO_PEERCRED` で接続元 uid を確かめる
必要があり（S-6.8）、受け入れたソケットを手元に置ける標準ライブラリのサーバが要るため。
HTTP/1.1、Content-Length の本文、keep-alive にだけ対応する（`ax` と `gate` の呼び出しで足りる）。
chunked の本文は 501 で断る。

- `Dispatcher`: 1 つの asyncio のイベントループを別スレッドで持ち、ASGI アプリの lifespan を上げる。
  要求ごとに `run_coroutine_threadsafe` で流す。どのスレッドから呼んでもよい。
- `HttpListener`: 待ち受け。AF_UNIX（commander ソケット。`umask 077` で bind、権限 0600、
  起動のたびに残っていたら外す）と AF_INET（healthz 用の TCP）の両方。
- `via` の確定: AF_UNIX のとき `via_name` があれば接続ごとに `vias.ident_for(via_name, uid)` を呼び、
  `Ident` を scope["ax_ident"] に置く。身元が決まらない接続は 403（`auth.failed`）で断つ。
  via_name が無い待ち受け（TCP の healthz など）は scope["ax_ident"] を置かない。
"""
from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import logging
import os
import signal
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import vias

log = logging.getLogger("axintake.httpsock")

MAX_LINE = 65536
MAX_HEADERS = 100
MAX_BODY = 10 * 1024 * 1024
READ_TIMEOUT_S = 65.0


class BadRequest(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def peercred(sock: socket.socket) -> tuple[int, int, int] | None:
    """接続相手の (pid, uid, gid)（Linux の SO_PEERCRED）。"""
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)
    except OSError:
        return None


class Dispatcher:
    """ASGI アプリを1つのイベントループで動かす係。"""

    def __init__(self, app: Any, *, on_auth_failed: Callable[[str, str], None] | None = None):
        self.app = app
        self.on_auth_failed = on_auth_failed
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lifespan_task: asyncio.Task | None = None
        self._lifespan_q: asyncio.Queue | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="axintake-asgi", daemon=True)
        self._thread.start()
        if not self._ready.wait(15):
            raise RuntimeError("ASGI のループが起きない")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._startup_lifespan())
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(self._shutdown_lifespan())
            with contextlib.suppress(Exception):
                loop.close()

    async def _startup_lifespan(self) -> None:
        """lifespan の startup だけ済ませる（heartbeat の最初の1回など）。shutdown は止まるとき。"""
        scope = {"type": "lifespan", "asgi": {"version": "1.0", "spec_version": "2.3"}}
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait({"type": "lifespan.startup"})
        done = asyncio.Event()
        ok: dict = {"v": None}

        async def receive():
            return await q.get()

        async def send(msg):
            if msg["type"] == "lifespan.startup.complete":
                ok["v"] = True
                done.set()
            elif msg["type"] == "lifespan.startup.failed":
                ok["v"] = False
                done.set()

        self._lifespan_task = asyncio.create_task(self.app(scope, receive, send))
        self._lifespan_q = q
        try:
            await asyncio.wait_for(done.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("ASGI アプリの lifespan の startup が済まない（lifespan 無しで続ける）")
        if ok["v"] is not True:
            if self._lifespan_task is not None:
                self._lifespan_task.cancel()
            self._lifespan_task = None
            self._lifespan_q = None

    async def _shutdown_lifespan(self) -> None:
        if self._lifespan_q is None or self._lifespan_task is None:
            return
        self._lifespan_q.put_nowait({"type": "lifespan.shutdown"})
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(self._lifespan_task), timeout=5)

    def shutdown(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def dispatch(self, scope: dict) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        """1 要求をアプリへ流して (status, headers, body) を返す。"""
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(self._call(scope), self._loop)
        return fut.result(timeout=60)

    async def _call(self, scope: dict) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        body_sent = {"n": 0}

        async def receive():
            n = body_sent["n"]
            body_sent["n"] += 1
            if n == 0:
                return {"type": "http.request", "body": scope.pop("_body", b""), "more_body": False}
            return {"type": "http.disconnect"}

        out: dict[str, Any] = {"start": None, "body": bytearray()}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["start"] = msg
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        await self.app(scope, receive, send)
        start = out["start"] or {"status": 500, "headers": []}
        return start["status"], list(start.get("headers") or []), bytes(out["body"])


def _write_response(wfile, status: int, headers: list[tuple[bytes, bytes]], body: bytes, *, keep_alive: bool, head_only: bool = False) -> None:
    reason = http.client.responses.get(status, "Unknown")
    lines = [f"HTTP/1.1 {status} {reason}\r\n".encode("latin-1")]
    have_len = any(k.lower() == b"content-length" for k, _ in headers)
    have_conn = any(k.lower() == b"connection" for k, _ in headers)
    for k, v in headers:
        lines.append(k + b": " + v + b"\r\n")
    if not have_len:
        lines.append(b"Content-Length: " + str(len(body)).encode() + b"\r\n")
    if not have_conn:
        lines.append((b"Connection: keep-alive\r\n" if keep_alive else b"Connection: close\r\n"))
    lines.append(b"\r\n")
    wfile.write(b"".join(lines))
    if not head_only:
        wfile.write(body)


def _simple(wfile, status: int, code: str, message: str, *, keep_alive: bool) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False).encode("utf-8")
    _write_response(wfile, status, [(b"Content-Type", b"application/json; charset=utf-8")], body, keep_alive=keep_alive)


def read_one_request(rfile) -> dict | None:
    """要求を 1 つ読む。接続が閉じたら None。形が壊れていれば BadRequest。"""
    line = rfile.readline(MAX_LINE + 1)
    if not line:
        return None
    if len(line) > MAX_LINE:
        raise BadRequest(414, "too_large", "要求の行が長すぎる")
    parts = line.decode("latin-1").rstrip("\r\n").split(" ")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        raise BadRequest(400, "bad_request", "要求の行が壊れている")
    method, target, version = parts
    headers = http.client.parse_headers(rfile)
    if len(headers.items()) > MAX_HEADERS:
        raise BadRequest(431, "too_large", "header が多すぎる")
    body = b""
    te = (headers.get("Transfer-Encoding") or "").lower()
    if "chunked" in te:
        raise BadRequest(501, "not_implemented", "chunked の本文は受けない")
    cl = headers.get("Content-Length")
    if cl is not None:
        try:
            n = int(cl)
        except ValueError:
            raise BadRequest(400, "bad_request", "Content-Length が数でない") from None
        if n < 0 or n > MAX_BODY:
            raise BadRequest(413, "too_large", "本文が大きすぎる")
        body = rfile.read(n) if n else b""
        if body is None or len(body) != n:
            raise BadRequest(400, "bad_request", "本文が短い")
    return {"method": method.upper(), "target": target, "version": version, "headers": headers, "body": body}


def build_scope(req: dict, *, scheme: str, server: tuple, ident=None) -> dict:
    target = req["target"]
    path, _, query = target.partition("?")
    if not path.startswith("/"):
        path = "/" + path  # proxy 形式の絶対 URL は来ない（入口の受け口は origin 形式）
    raw_headers = req["headers"].raw_items()
    headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in raw_headers]
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "1.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": req["method"],
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": query.encode("latin-1"),
        "root_path": "",
        "headers": headers,
        "client": None,
        "server": server,
        "_body": req["body"],
    }
    if ident is not None:
        scope["ax_ident"] = ident
    return scope


def connection_loop(conn: socket.socket, *, dispatcher: Dispatcher, scheme: str, server: tuple,
                    via_name: str | None, on_request=None) -> None:
    """1 接続。keep-alive の間、要求を繰り返し受け付ける。"""
    ident = None
    if via_name is not None:
        creds = peercred(conn)
        uid = creds[1] if creds else -1
        ident = vias.ident_for(via_name, uid)
    try:
        conn.settimeout(READ_TIMEOUT_S)
        rfile = conn.makefile("rb", buffering=64 * 1024)
        wfile = conn.makefile("wb", buffering=64 * 1024)
        try:
            if via_name is not None and ident is None:
                if dispatcher.on_auth_failed is not None:
                    with contextlib.suppress(Exception):
                        dispatcher.on_auth_failed(via_name, "uid_mismatch")
                _simple(wfile, 403, "auth.failed", "受け口の資格が確かめられない", keep_alive=False)
                wfile.flush()
                return
            while True:
                try:
                    req = read_one_request(rfile)
                except BadRequest as e:
                    _simple(wfile, e.status, e.code, e.message, keep_alive=False)
                    wfile.flush()
                    return
                if req is None:
                    return
                conn_hdr = (req["headers"].get("Connection") or "").lower()
                keep_alive = (conn_hdr != "close")
                if on_request is not None:
                    with contextlib.suppress(Exception):
                        on_request(req)
                scope = build_scope(req, scheme=scheme, server=server, ident=ident)
                status = 500
                headers: list[tuple[bytes, bytes]] = []
                body = b""
                try:
                    status, headers, body = dispatcher.dispatch(scope)
                except Exception:  # noqa: BLE001
                    log.exception("要求の処理に失敗")
                    _simple(wfile, 500, "internal", "内部エラー", keep_alive=keep_alive)
                    wfile.flush()
                    if not keep_alive:
                        return
                    continue
                _write_response(wfile, status, headers, body, keep_alive=keep_alive, head_only=(req["method"] == "HEAD"))
                wfile.flush()
                if not keep_alive:
                    return
        finally:
            with contextlib.suppress(Exception):
                rfile.close()
            with contextlib.suppress(Exception):
                wfile.close()
    finally:
        with contextlib.suppress(Exception):
            conn.close()


class HttpListener:
    """待ち受け 1 つ（AF_UNIX か AF_INET）。accept のたびにスレッドを1つ作る。"""

    def __init__(self, *, name: str, dispatcher: Dispatcher, unix_path: str | Path | None = None,
                 host: str | None = None, port: int | None = None, via_name: str | None = None,
                 backlog: int = 64, on_request=None, on_auth_failed=None):
        if (unix_path is None) == (host is None):
            raise ValueError("unix_path か host:port のどちらか一方だけを指定する")
        self.name = name
        self.dispatcher = dispatcher
        self.unix_path = str(unix_path) if unix_path else None
        self.host, self.port = host, port
        self.via_name = via_name
        self.backlog = backlog
        self.on_request = on_request
        self.on_auth_failed = on_auth_failed
        self.sock: socket.socket | None = None
        self.listening = False
        self.refusal: str | None = None  # 待ち受けられなかった理由（health.alert 用）
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- ソケットの作り方（F-9.2 の (3)）----
    def _prepare(self) -> None:
        if self.unix_path is not None:
            p = Path(self.unix_path)
            d = p.parent
            try:
                st = os.stat(d)
            except OSError as e:
                self.refusal = f"dir_missing:{type(e).__name__}"
                return
            if st.st_uid != os.geteuid() or (st.st_mode & 0o077):
                # ディレクトリの所有者が自分でない、または group・other に権限があるときは待ち受けない
                self.refusal = "dir_owner_or_mode"
                return
            if os.path.lexists(self.unix_path):
                if os.path.isdir(self.unix_path) and not os.path.islink(self.unix_path):
                    self.refusal = "path_not_socket"
                    return
                try:
                    os.unlink(self.unix_path)  # 残っていれば自分の古いソケットとして外す
                except OSError as e:
                    self.refusal = f"unlink_failed:{type(e).__name__}"
                    return
            old_umask = os.umask(0o077)
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    s.bind(self.unix_path)
                    os.chmod(self.unix_path, 0o600)
                    s.listen(self.backlog)
                except OSError:
                    with contextlib.suppress(Exception):
                        s.close()
                    raise
            except OSError as e:
                self.refusal = f"bind_failed:{type(e).__name__}"
                return
            finally:
                os.umask(old_umask)
            self.sock = s
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((self.host, self.port))
                s.listen(self.backlog)
            except OSError as e:
                self.refusal = f"bind_failed:{type(e).__name__}"
                with contextlib.suppress(Exception):
                    s.close()
                return
            self.sock = s

    def start(self) -> bool:
        """待ち受けを始める。できなければ False（health.alert は呼び出し側が書く）。"""
        self._prepare()
        if self.sock is None:
            return False
        self.listening = True
        self._thread = threading.Thread(target=self._serve, name=f"axintake-{self.name}", daemon=True)
        self._thread.start()
        return True

    def _serve(self) -> None:
        assert self.sock is not None
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                log.exception("accept の失敗（待ち受けを続ける）")
                time.sleep(0.1)
                continue
            scheme = "http"
            server: tuple = ("unix", self.unix_path or "") if self.unix_path else (self.host, self.port)
            t = threading.Thread(target=connection_loop, kwargs={
                "conn": conn, "dispatcher": self.dispatcher, "scheme": scheme, "server": server,
                "via_name": self.via_name, "on_request": self.on_request}, daemon=True)
            t.start()

    def stop(self) -> None:
        self._stop.set()
        if self.sock is not None:
            with contextlib.suppress(Exception):
                self.sock.close()
        if self.unix_path and self.listening:
            with contextlib.suppress(OSError):
                os.unlink(self.unix_path)  # 自分が作ったソケットのファイルだけ外す
        self.listening = False


def run_until_signal(listeners: list[HttpListener], dispatcher: Dispatcher) -> None:
    """SIGTERM か SIGINT まで待ち、降りる（D-5: 他の部品を止めない）。"""
    stop = {"flag": False}

    def _handle(signum, frame):
        stop["flag"] = True

    old_term = signal.signal(signal.SIGTERM, _handle)
    old_int = signal.signal(signal.SIGINT, _handle)
    try:
        while not stop["flag"]:
            time.sleep(0.2)
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        for l in listeners:
            with contextlib.suppress(Exception):
                l.stop()
        dispatcher.shutdown()
