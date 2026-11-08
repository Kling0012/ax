"""各中核の API の薄いクライアント（D-2.2 axcommon.client）。標準ライブラリだけ（urllib）。

転送は Transport（呼び出し可能）で差し替えられる。試験は tests/fakes/http.py の FakeTransport を渡す（ネットワークを使わない）。
  transport(method, url, headers, body_bytes_or_None, timeout) -> (status, headers_dict, body_bytes)
転送の失敗（接続できない、時間切れ）は Unavailable。HTTP の 4xx は ApiError、5xx は ServerError（Unavailable の一種として扱い、送る側は溜める）。
資格: token を渡すと Authorization: Bearer <token> を付ける。ログに token を出さない。
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from typing import Any, Callable

Transport = Callable[[str, str, dict, "bytes | None", float], "tuple[int, dict, bytes]"]
DEFAULT_TIMEOUT_S = 10.0
LONGPOLL_NETWORK_MARGIN_S = 10.0


class Unavailable(Exception):
    """転送できない（接続拒否、名前解決、時間切れ、5xx）。送る側は outbox に溜めて続ける（S-2.2）。"""


class ApiError(Exception):
    """4xx。code は応答の {"error": {"code": …}}。"""

    def __init__(self, status: int, code: str, message: str = "", body: Any = None):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status, self.code, self.message, self.body = status, code, message, body


class ServerError(Unavailable):
    def __init__(self, status: int, body: bytes = b""):
        super().__init__(f"HTTP {status}")
        self.status = status


def urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (http/https の URL だけを渡す呼び出し側の責任)
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() or b""
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise Unavailable(f"{type(e).__name__}") from e


class ApiClient:
    def __init__(self, base_url: str, token: str | None = None, *, transport: Transport | None = None, timeout: float = DEFAULT_TIMEOUT_S,
                 extra_headers: dict | None = None):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url は http:// か https:// で始める")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport or urllib_transport
        self.timeout = timeout
        self._extra = dict(extra_headers or {})

    def __repr__(self) -> str:  # token を出さない
        return f"ApiClient({self.base_url!r}, token={'set' if self._token else 'none'})"

    def request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None, ok_statuses: tuple = (), timeout: float | None = None) -> Any:
        url = self.base_url + path
        if params:
            from urllib.parse import urlencode

            url += ("&" if "?" in url else "?") + urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Accept": "application/json", **self._extra}
        body = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        status, _h, raw = self._transport(method, url, headers, body, self.timeout if timeout is None else timeout)
        if status >= 500:
            raise ServerError(status, raw)
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except ValueError:
                parsed = None
        if 200 <= status < 300 or status in ok_statuses:
            return parsed
        err = (parsed or {}).get("error", {}) if isinstance(parsed, dict) else {}
        raise ApiError(status, err.get("code", "http_%d" % status), err.get("message", ""), parsed)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, json_body=body, **kw)

    def put(self, path, body=None, **kw):
        return self.request("PUT", path, json_body=body, **kw)

    def healthz(self) -> dict:
        return self.get("/healthz")


def service_url(name: str, port: int = 8080, environ: dict | None = None) -> str:
    """部品の URL。環境変数 AX_<NAME>_URL（NAME は大文字、ハイフンは _）が優先。既定は http://<name>:<port>（compose の網の中の名前）。
    作業側の口（8081）を使う部品は port=8081 を渡す（F-18.4）。"""
    environ = os.environ if environ is None else environ
    key = "AX_" + name.upper().replace("-", "_") + "_URL"
    return environ.get(key) or f"http://{name}:{port}"


class TimelineClient(ApiClient):
    """時系列記録（S-3.5）。"""

    def post_events(self, events: "dict | list[dict]") -> Any:
        return self.post("/v1/events", events)

    def get_events(self, *, after=None, kind=None, subloop=None, project=None, subject=None, limit=None, wait=None) -> Any:
        timeout = self.timeout
        try:
            wait_s = float(wait) if wait is not None else 0.0
        except (TypeError, ValueError):
            wait_s = 0.0  # Let the server reject invalid parameters.
        if math.isfinite(wait_s) and wait_s > 0:
            timeout = max(timeout, wait_s + LONGPOLL_NETWORK_MARGIN_S)
        return self.get("/v1/events", params={"after": after, "kind": kind, "subloop": subloop, "project": project, "subject": subject,
                                              "limit": limit, "wait": wait}, timeout=timeout)

    def get_event(self, event_id: str) -> Any:
        return self.get(f"/v1/events/{event_id}")

    def projection(self, name: str, **params) -> Any:
        return self.get(f"/v1/projections/{name}", params=params)


class LedgerClient(ApiClient):
    """台帳（S-9.3）。サブループは作業側の口（8081）で create_todo と読み取りだけ使える（F-18.4）。"""

    def create_todo(self, todo: dict) -> Any:
        return self.post("/v1/todos", todo)

    def get_todo(self, todo_id: str) -> Any:
        return self.get(f"/v1/todos/{todo_id}")

    def list_todos(self, **params) -> Any:
        return self.get("/v1/todos", params=params)

    def transition(self, todo_id: str, to: str, reason: str | None = None, **extra) -> Any:
        return self.post(f"/v1/todos/{todo_id}/transition", {"to": to, **({"reason": reason} if reason else {}), **extra})

    def answer(self, todo_id: str, body: dict) -> Any:
        return self.post(f"/v1/todos/{todo_id}/answer", body)


class ControlClient(ApiClient):
    """制御 API（S-5.7）。プラグインは control_poll_s ごとに effective を読む（S-4.4）。"""

    def effective(self, plugin_id: str) -> Any:
        return self.get(f"/v1/plugins/{plugin_id}/effective")

    def plugins(self) -> Any:
        return self.get("/v1/plugins")

    def issue(self, command: dict) -> Any:
        return self.post("/v1/commands", command)

    def validate(self, kind: str, body: Any) -> Any:
        return self.post("/v1/validate", {"kind": kind, "body": body})


class AuthenticatedPoolClient(ApiClient):
    """Reject pool requests locally when the caller's credential is missing."""

    def request(self, method, path, **kw):
        if not self._token:
            raise Unavailable("pool_credential_missing")
        return super().request(method, path, **kw)


class WorkerPoolClient(AuthenticatedPoolClient):
    """worker-pool（S-8.8）。"""

    def submit(self, job: dict) -> Any:
        return self.post("/v1/jobs", job)

    def job(self, job_id: str) -> Any:
        return self.get(f"/v1/jobs/{job_id}")

    def cancel(self, job_id: str) -> Any:
        return self.post(f"/v1/jobs/{job_id}/cancel", {})
