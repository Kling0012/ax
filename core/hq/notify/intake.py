"""入口の `hq` 口への指示（S-12.1: 画面のすべての操作は入口 `from=hq` を通す）。

HQ のコンテナは `hq-intake/intake.sock`（F-13.1。コンテナ内 `/run/hq-intake/intake.sock`）へ Unix ソケットの HTTP で POST する。
接続元の確認（uid が AX_HQ_UID）は入口の側（S-6.8）。ここは指示の本文を作って送るだけ。
転送は差し替えられる: transport(method, path, body_bytes) -> (status, body_bytes)。試験は偽の入口を渡す（ソケットを使わない）。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
from typing import Callable

from axcommon import ids

Transport = Callable[[str, str, "bytes | None"], "tuple[int, bytes]"]
DEFAULT_SOCKET = "/run/hq-intake/intake.sock"


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("intake", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def unix_transport(path: str | None = None, timeout: float = 30.0) -> Transport:
    sock_path = path or os.environ.get("AX_HQ_INTAKE_SOCKET") or DEFAULT_SOCKET

    def call(method: str, url_path: str, body: bytes | None, extra_headers=None) -> tuple[int, bytes]:
        conn = _UnixConnection(sock_path, timeout)
        try:
            conn.request(method, url_path, body=body, headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json", **(extra_headers or {})})
            r = conn.getresponse()
            return r.status, r.read()
        except (OSError, http.client.HTTPException) as e:
            raise ConnectionError(type(e).__name__) from e
        finally:
            conn.close()

    return call


class IntakeError(Exception):
    """入口に届かない。"""


class IntakeClient:
    def __init__(self, transport: Transport | None = None):
        self._t = transport or unix_transport()

    def submit(self, destination: str, payload: dict, *, idempotency_key: str, confirm_irreversible: bool = False,
               session: str | None = None) -> tuple[int, dict]:
        body: dict = {"id": ids.new_ulid(), "from": "hq", "destination": destination, "payload": payload,
                      "idempotency_key": idempotency_key[:200]}
        if confirm_irreversible:
            body["confirm_irreversible"] = True
        if session:
            body["session"] = session[:64]
        try:
            status, raw = self._t("POST", "/v1/instructions", json.dumps(body, ensure_ascii=False).encode("utf-8"))
        except ConnectionError as e:
            raise IntakeError(str(e)) from e
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            parsed = {}
        return status, parsed if isinstance(parsed, dict) else {}
