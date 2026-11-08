"""seat/hq/gate の待ち受け。1 つの失敗で他の受け口を止めない。"""
from __future__ import annotations

import contextlib
import errno
import os
from pathlib import Path
import socket
import stat
import threading

from axcommon import ids
from core.intake import httpsock
from .store import SeatError


class Connection:
    def __init__(self, listener, conn):
        self.listener, self.conn, self.ident = listener, conn, None
        self.session = ids.new_ulid()
        self.reads = self.writes = self.denied = 0

    def bind(self, ident):
        if self.ident is not None and self.ident.principal != ident.principal:
            raise SeatError("forged_authority")
        if self.ident is None:
            self.listener.service.open_session(ident)
            self.ident = ident
            self.listener.service.emit("commander.session.opened", ident.principal,
                                        {"via": ident.via, "principal": ident.principal, "session": self.session})

    def record(self, method, status):
        self.reads += method == "GET"
        self.writes += method == "POST"
        self.denied += status >= 400

    def close(self):
        with contextlib.suppress(OSError):
            self.conn.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.conn.close()


class SeatListener(httpsock.HttpListener):
    def __init__(self, *, service, group_id=None, socket_factory=socket.socket, **kwargs):
        super().__init__(**kwargs)
        self.service, self.group_id, self.socket_factory = service, group_id, socket_factory
        self.connections = set()
        self.connections_lock = threading.RLock()
        self._owned_path = None

    @staticmethod
    def _archive_socket(path):
        archive = path.parent / "archive"
        archive.mkdir(mode=0o700, exist_ok=True)
        info = archive.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("socket archive is not private")
        destination = archive / ids.new_ulid()
        destination.mkdir(mode=0o700)
        os.rename(path, destination / path.name)

    @staticmethod
    def _is_stale(path):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                probe.connect(str(path))
            except OSError as error:
                if error.errno == errno.ECONNREFUSED:
                    return True
                raise
        return False

    def _prepare(self):
        sock = None
        try:
            if self.unix_path is not None:
                path = Path(self.unix_path)
                directory = path.parent.stat()
                if path.parent.is_symlink() or directory.st_uid != os.geteuid() or stat.S_IMODE(directory.st_mode) != 0o2710 or self.group_id is None or directory.st_gid != self.group_id:
                    self.refusal = "dir_owner_or_mode"
                    return
                if path.exists() or path.is_symlink():
                    # 残った自分のソケットだけ archive へ移す。通常ファイルは触らない。
                    previous = path.lstat()
                    if not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.geteuid():
                        self.refusal = "path_not_owned_socket"
                        return
                    if not self._is_stale(path):
                        self.refusal = "socket_in_use"
                        return
                    current = path.lstat()
                    if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
                        self.refusal = "socket_changed"
                        return
                    self._archive_socket(path)
                sock = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.bind(self.unix_path)
                os.chown(self.unix_path, -1, self.group_id)
                os.chmod(self.unix_path, 0o660)
                own = path.stat()
                self._owned_path = (own.st_dev, own.st_ino)
            else:
                if not self.host:
                    self.refusal = "gate_bind_address_missing"
                    return
                sock = self.socket_factory(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.host, self.port))
            sock.listen(self.backlog)
            self.sock = sock
        except (OSError, ValueError) as error:
            self.refusal = "bind_failed:" + type(error).__name__
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()

    def _serve(self):
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, address = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self._stop.is_set():
                    self.service.store._alert("seat_accept_failed:" + self.name)
                continue
            thread = threading.Thread(target=self.handle_connection, args=(conn, address), daemon=True)
            thread.start()

    def handle_connection(self, conn, address=None):
        connection = Connection(self, conn)
        with self.connections_lock:
            self.connections.add(connection)
        ident = None
        if self.via_name != "tailnet":
            ident = self.service.authenticate_connection(self.via_name, conn)
        rfile = wfile = None
        try:
            conn.settimeout(httpsock.READ_TIMEOUT_S)
            rfile, wfile = conn.makefile("rb"), conn.makefile("wb")
            if self.via_name != "tailnet" and ident is None:
                httpsock._simple(wfile, 403, "auth.failed", "", keep_alive=False)
                wfile.flush()
                return
            while not self._stop.is_set():
                try:
                    request = httpsock.read_one_request(rfile)
                except httpsock.BadRequest as error:
                    httpsock._simple(wfile, error.status, error.code, "", keep_alive=False)
                    wfile.flush()
                    return
                if request is None:
                    return
                scope = httpsock.build_scope(request, scheme="http", server=("unix", self.unix_path) if self.unix_path else (self.host, self.port), ident=ident)
                scope.update(_seat_via=self.via_name, _seat_peer=address[0] if address else "",
                             _seat_session=connection.session, _seat_connection=connection)
                status, headers, body = self.dispatcher.dispatch(scope)
                keep_alive = (request["headers"].get("Connection", "").lower() != "close" and status not in {401, 403, 503})
                httpsock._write_response(wfile, status, headers, body, keep_alive=keep_alive)
                wfile.flush()
                if not keep_alive:
                    return
        except (OSError, ValueError):
            return
        except Exception:
            self.service.store._alert("seat_connection_failed:" + self.name)
        finally:
            for stream in (rfile, wfile):
                if stream:
                    with contextlib.suppress(OSError):
                        stream.close()
            connection.close()
            with self.connections_lock:
                self.connections.discard(connection)
            if connection.ident:
                self.service.close_session(connection.ident)
                self.service.emit("commander.session.closed", connection.ident.principal,
                                  {"session": connection.session, "reads": connection.reads,
                                   "writes": connection.writes, "denied": connection.denied})

    def close_principal(self, principal, reason):
        with self.connections_lock:
            matching = [conn for conn in self.connections if conn.ident and conn.ident.principal == principal]
        for conn in matching:
            conn.close()

    def stop(self):
        self._stop.set()
        if self.sock:
            with contextlib.suppress(OSError):
                self.sock.close()
        with self.connections_lock:
            current = list(self.connections)
        for connection in current:
            connection.close()
        if self.unix_path and self._owned_path:
            with contextlib.suppress(OSError):
                path = Path(self.unix_path)
                own = path.lstat()
                if (own.st_dev, own.st_ino) == self._owned_path:
                    self._archive_socket(path)
        self.listening = False
