"""Streamable HTTP MCP client. Transport is injected; this module opens no ports."""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any


class BoxError(Exception):
    """Only stable codes may cross the driver boundary, never remote error text."""

    def __init__(self, code: str, *, retry_after: float | None = None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


class ToolDenied(BoxError):
    def __init__(self, deny_reason: str = "not_in_table"):
        self.deny_reason = deny_reason
        super().__init__("tool_denied")


class IntegrationRequired(NotImplementedError):
    pass


def decode_response(headers: dict, body: bytes, request_id: int) -> dict:
    """Read JSON or a finite SSE response, ignoring unrelated notifications."""
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    try:
        if "text/event-stream" in content_type:
            messages = []
            data: list[str] = []
            for line in body.decode("utf-8").splitlines() + [""]:
                if not line:
                    if data:
                        messages.append(json.loads("\n".join(data)))
                        data = []
                elif line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
        else:
            messages = [json.loads(body)]
        for msg in messages:
            if isinstance(msg, dict) and msg.get("id") == request_id:
                if "error" in msg:
                    raise BoxError("tool_error")
                if "result" in msg and isinstance(msg["result"], dict):
                    return msg["result"]
        raise BoxError("invalid_response")
    except (ValueError, UnicodeError, TypeError):
        raise BoxError("invalid_response") from None


def tool_payload(result: dict) -> Any:
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content", [])
    if not isinstance(content, list):
        raise BoxError("invalid_response")
    texts = [v["text"] for v in content if isinstance(v, dict) and v.get("type") == "text" and isinstance(v.get("text"), str)]
    text = "\n".join(texts)
    try:
        return json.loads(text)
    except ValueError:
        return text


class MCPSession:
    """One instance per environment, serial requests and one session reset retry.

    transport(method, url, headers, body, timeout) -> (status, headers, bytes)
    key_reader() -> key. The caller binds this to read_key('box', 'ax').
    """

    def __init__(self, url: str, transport: Callable, key_reader: Callable,
                 *, timeout_s: float = 120, clock: Callable = time.monotonic,
                 emit: Callable | None = None, env_id: str = "box"):
        self.url, self.transport, self._key_reader = url, transport, key_reader
        self.timeout_s, self.clock, self.emit = timeout_s, clock, emit
        self.env_id = env_id
        self._lock = threading.RLock()
        self._session_id: str | None = None
        self._key: str | None = None
        self._sequence = 0
        self.initialized = False
        self._context = threading.local()
        self.job_id = "probe"

    @property
    def job_id(self):
        return getattr(self._context, "job_id", "probe")

    @job_id.setter
    def job_id(self, value):
        self._context.job_id = value

    def __repr__(self):
        return f"MCPSession(env_id={self.env_id!r}, initialized={self.initialized})"

    def _event(self, name: str, size_in: int, size_out: int, code: str, started: float):
        if self.emit is not None:
            payload = {"job_id": getattr(self._context, "job_id", self.job_id), "path": "J", "env_id": self.env_id,
                       "tool": name, "args_bytes": size_in, "response_bytes": size_out,
                       "result": code, "duration_s": max(0, self.clock() - started)}
            self.emit("sandbox.call", payload)

    def _request(self, method: str, params: dict, timeout_s: float, *, notification=False) -> dict:
        if not 0 < timeout_s <= self.timeout_s:
            raise BoxError("timeout_out_of_range")
        if self._key is None:
            raise BoxError("credential_missing")
        self._sequence += 1
        request_id = self._sequence
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = request_id
        raw = json.dumps(message, separators=(",", ":")).encode()
        headers = {"Authorization": "Bearer " + self._key,
                   "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        name = params.get("name", method)
        args_size = len(json.dumps(params.get("arguments", params), separators=(",", ":")).encode())
        started, size_out, outcome = self.clock(), 0, "ok"
        try:
            try:
                status, response_headers, body = self.transport("POST", self.url, headers, raw, timeout_s)
            except TimeoutError:
                raise BoxError("timed_out") from None
            except Exception:
                raise BoxError("unavailable") from None
            size_out = len(body)
            if status == 404:
                raise BoxError("session_expired")
            if status in (401, 403):
                raise BoxError("auth_failed")
            if status == 402:
                raise BoxError("quota")
            if status == 429:
                value = next((v for k, v in response_headers.items() if k.lower() == "retry-after"), "60")
                try:
                    retry_after = max(0, float(value))
                except (ValueError, TypeError):
                    retry_after = 60
                raise BoxError("rate_limited", retry_after=retry_after)
            if status >= 500:
                raise BoxError("unavailable")
            if status not in (200, 202, 204):
                raise BoxError("invalid_response")
            sid = next((v for k, v in response_headers.items() if k.lower() == "mcp-session-id"), None)
            if sid is not None:
                self._session_id = sid
            if notification:
                return {}
            if status != 200:
                raise BoxError("invalid_response")
            result = decode_response(response_headers, body, request_id)
            if result.get("isError"):
                outcome = "tool_error"
            return result
        except BoxError as error:
            outcome = error.code
            raise
        finally:
            self._event(name, args_size, size_out, outcome, started)

    def _initialize(self):
        # Re-read on every logical request. Revocation must affect an existing session.
        self._request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                     "clientInfo": {"name": "ax-resources", "version": "1"}}, self.timeout_s)
        self._request("notifications/initialized", {}, self.timeout_s, notification=True)
        self.initialized = True

    def request(self, method: str, params: dict | None = None, *, timeout_s: float | None = None,
                job_id: str | None = None) -> dict:
        with self._lock:
            previous_job = self.job_id
            self.job_id = previous_job if job_id is None else job_id
            try:
                try:
                    key = self._key_reader()
                except (BoxError, IntegrationRequired):
                    raise
                except Exception as error:
                    code = getattr(error, "code", "credential_invalid")
                    if code not in {"credential_missing", "credential_invalid", "credential_insecure", "credential_purpose_mismatch"}:
                        code = "credential_invalid"
                    raise BoxError(code) from None
                if not isinstance(key, str) or not 16 <= len(key) <= 512 or not key.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in key) or key.startswith("Bearer "):
                    raise BoxError("credential_invalid")
                if key != self._key:
                    self._key, self.initialized, self._session_id = key, False, None
                for attempt in range(2):
                    try:
                        if not self.initialized:
                            self._initialize()
                        if method == "initialize":
                            return {}
                        return self._request(method, params or {}, self.timeout_s if timeout_s is None else timeout_s)
                    except BoxError as error:
                        if error.code != "session_expired" or attempt:
                            raise
                        self.initialized, self._session_id = False, None
                raise BoxError("session_expired")
            finally:
                self.job_id = previous_job
