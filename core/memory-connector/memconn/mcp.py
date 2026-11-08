"""agent-memory の MCP クライアント（Streamable HTTP。F-8）。

手順: POST <endpoint>（Accept: application/json, text/event-stream）で initialize → 応答ヘッダーの Mcp-Session-Id を以後に付ける →
notifications/initialized → tools/call。応答は JSON か SSE（data: 行）。認証ヘッダーは要らない。
転送は axcommon.client の Transport と同じ形（method, url, headers, body, timeout）-> (status, headers, body)。試験は偽の MCP を渡す。

失敗の区別（F-8「送信の失敗」、timeout や切断を保存の失敗と決めつけない）:
  NotSent        接続できなかった（要求が相手に届いていない）。溜めて後で送り直してよい。
  Indeterminate  要求を送った後の時間切れ・切断。保存されたかどうか不明。書き込みの呼び出しでは再送しない。
  ToolError      相手が処理して失敗を返した（isError）。
"""
from __future__ import annotations

import errno
import itertools
import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable

from axcommon.client import Transport, Unavailable


class NotSent(Unavailable):
    pass


class Indeterminate(Unavailable):
    pass


class ToolError(Exception):
    pass


def http_transport(method, url, headers, body, timeout):
    """urllib の転送。接続前の失敗は NotSent、送った後の失敗は Indeterminate にする。"""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() or b""
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror, socket.timeout, TimeoutError)) or \
                (isinstance(reason, OSError) and getattr(reason, "errno", None) in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ECONNREFUSED)):
            raise NotSent(type(reason).__name__) from e
        raise Indeterminate(type(reason).__name__) from e
    except (TimeoutError, ConnectionError, OSError) as e:
        raise Indeterminate(type(e).__name__) from e


def _parse_body(headers: dict, raw: bytes, want_id: int | None) -> dict | None:
    ctype = ""
    for k, v in headers.items():
        if k.lower() == "content-type":
            ctype = v.lower()
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in ctype or text.lstrip().startswith(("event:", "data:")):
        found = None
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(msg, dict) and (want_id is None or msg.get("id") == want_id):
                    found = msg
        return found
    if not text.strip():
        return None
    try:
        msg = json.loads(text)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


class McpClient:
    def __init__(self, endpoint: str, *, transport: Transport | None = None, timeout: float = 30.0, client_name: str = "ax-memory-connector"):
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint は http:// か https:// で始める")
        self.endpoint, self.timeout = endpoint, timeout
        self._t = transport or http_transport
        self._ids = itertools.count(1)
        self.session_id: str | None = None
        self.client_name = client_name

    def _post(self, payload: dict, *, want_response: bool = True) -> tuple[int, dict, dict | None]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        status, rh, raw = self._t("POST", self.endpoint, headers, json.dumps(payload, ensure_ascii=False).encode("utf-8"), self.timeout)
        return status, rh, (_parse_body(rh, raw, payload.get("id")) if want_response else None)

    def initialize(self) -> None:
        """セッションを開く。ここまでの失敗は、書き込みの要求がまだ出ていないので NotSent として扱ってよい。"""
        rid = next(self._ids)
        try:
            status, rh, msg = self._post({"jsonrpc": "2.0", "id": rid, "method": "initialize",
                                          "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                                     "clientInfo": {"name": self.client_name, "version": "1"}}})
        except Indeterminate as e:
            raise NotSent("initialize: " + str(e)) from e
        if status >= 500 or status in (401, 403, 404) or msg is None or "error" in msg:
            raise NotSent(f"initialize が通らない（HTTP {status}）")
        sid = next((v for k, v in rh.items() if k.lower() == "mcp-session-id"), None)
        self.session_id = sid
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, want_response=False)
        except Unavailable:
            pass  # 通知の失敗は無視する（応答を待たない通知）

    def call_tool(self, name: str, arguments: dict, *, write: bool = False) -> Any:
        """ツールを呼び、結果の JSON（content の text を JSON として読む。無ければ text 全体）を返す。
        write=True の呼び出しで、要求を送った後に切れたら Indeterminate（再送しない）。read は NotSent に読み替える（再送してよい）。"""
        if self.session_id is None:
            self.initialize()
        for attempt in (0, 1):
            rid = next(self._ids)
            try:
                status, _rh, msg = self._post({"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            except NotSent:
                raise
            except Indeterminate as e:
                if write:
                    raise
                raise NotSent("read: " + str(e)) from e
            if attempt == 0 and (status == 404 or (status == 400 and msg is None)):
                # セッションが切れた応答。要求は処理されていないので、開き直して 1 回だけ送り直してよい
                self.session_id = None
                self.initialize()
                continue
            break
        if status >= 500:
            if write:
                raise Indeterminate(f"HTTP {status}")
            raise NotSent(f"HTTP {status}")
        if msg is None:
            if write:
                raise Indeterminate("応答を読めない")
            raise NotSent("応答を読めない")
        if "error" in msg:
            raise ToolError(str(msg["error"].get("message", "error"))[:200])
        res = msg.get("result") or {}
        if res.get("isError"):
            raise ToolError(_text_of(res)[:200])
        if isinstance(res.get("structuredContent"), (dict, list)):
            return res["structuredContent"]
        text = _text_of(res)
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}


def _text_of(res: dict) -> str:
    return "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict) and c.get("type") == "text")


def find_memory_id(obj: Any) -> str | None:
    """応答の形が文書で決まっていないので、memory_id か id を再帰的に探す。"""
    if isinstance(obj, dict):
        for k in ("memory_id", "id"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
        for v in obj.values():
            r = find_memory_id(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_memory_id(v)
            if r:
                return r
    return None
