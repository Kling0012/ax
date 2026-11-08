"""F-9.2: 呼ぶたびに接続し、intake 以外の部品には接続しない。"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
from urllib.parse import urlsplit


class Unreachable(Exception):
    pass


class ResultUnknown(Exception):
    pass


def scalar(path, key):
    """F-9.2 の1行の socket 値。YAML のコードやタグは評価しない。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    match = re.search(r"^" + re.escape(key) + r"\s*:\s*(.+?)\s*$", text, re.MULTILINE)
    if match is None:
        return None
    value = match.group(1)
    if value.startswith(('"', "'")):
        end = value.find(value[0], 1)
        if end < 0 or value[end + 1:].strip().split(" ", 1)[0] not in {"", "#"}:
            raise ValueError("設定の値を読めない")
        return value[1:end]
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def socket_path(environ, home):
    value = environ.get("AX_SOCKET") or scalar(home / ".config/ax/config.yaml", "socket")
    value = value or str(home / ".local/state/ax/commander/commander.sock")
    if value == "~" or value.startswith("~/"):
        return str(home) + value[1:]
    return value


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
        except BaseException:
            self.sock.close()
            self.sock = None
            raise


def gate_token(environ, home):
    if environ.get("AX_GATE_TOKEN"):
        return environ["AX_GATE_TOKEN"]
    path = home / ".config/ax/gate-token"
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
        raise ValueError("gate-token の所有者か権限が違う。権限は600にする")
    return path.read_text(encoding="utf-8").strip()


class EntryTransport:
    def __init__(self, environ=None, home=None, *, connection_factory=None):
        self.environ = os.environ if environ is None else environ
        self.home = Path.home() if home is None else Path(home)
        self.connection_factory = connection_factory

    @property
    def target(self):
        raw = self.environ.get("AX_GATE_URL")
        if raw:
            # URL の userinfo や query に秘密があっても表示しない。
            parsed = urlsplit(raw)
            return "http://" + (parsed.hostname or "(URL が不正)") + ":" + str(parsed.port or 8909)
        return socket_path(self.environ, self.home)

    def __call__(self, method, path, body=None, headers=None):
        headers = {"Accept": "application/json", **(headers or {})}
        gate = self.environ.get("AX_GATE_URL")
        if gate:
            url = urlsplit(gate)
            if url.scheme != "http" or not url.hostname or "@" in url.netloc or url.path not in {"", "/"} or url.query or url.fragment:
                raise ValueError("AX_GATE_URL は http://名前:ポート の形にする")
            headers["Authorization"] = "Bearer " + gate_token(self.environ, self.home)
            connection = self.connection_factory(url.hostname, url.port or 8909) if self.connection_factory else http.client.HTTPConnection(url.hostname, url.port or 8909, timeout=30)
        else:
            target = socket_path(self.environ, self.home)
            connection = self.connection_factory(target) if self.connection_factory else UnixConnection(target)
        try:
            try:
                connection.connect()
            except (OSError, http.client.HTTPException) as exc:
                raise Unreachable(type(exc).__name__) from exc
            try:
                raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
                if raw is not None:
                    headers["Content-Type"] = "application/json"
                connection.request(method, path, body=raw, headers=headers)
                response = connection.getresponse()
                # 大きな応答は CLI 側で省略し、全件をメモリへ読まない。
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    if method != "GET":
                        raise ResultUnknown("応答の上限を超えた")
                    return response.status, {"truncated": True}
                try:
                    return response.status, json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    if method != "GET":
                        raise ResultUnknown("応答の JSON を読めない") from exc
                    raise ValueError("入口の応答が JSON でない") from exc
            except (OSError, http.client.HTTPException) as exc:
                if method != "GET":
                    raise ResultUnknown(type(exc).__name__) from exc
                raise Unreachable(type(exc).__name__) from exc
        finally:
            connection.close()
