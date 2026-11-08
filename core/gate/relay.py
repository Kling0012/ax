"""Small, explicit HTTP relay from gate to intake:8082.

Only ``Gate`` creates :class:`RelayRequest`. The header builder below is shared
with tests so the trust facts decoded by intake cannot drift from gate output.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json

from .contracts import RelayEvidence, RelayRequest, Reply, unresolved_transport

Transport = Callable[[RelayRequest, float], Awaitable[Reply]]
_DECLARED_HEADERS = {"agent": "X-AX-Agent", "model": "X-AX-Model", "session": "X-AX-Session"}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def wire_headers(request: RelayRequest, body: bytes, *, host: str, port: int) -> list[tuple[str, str]]:
    """Encode only gate-verified facts; reject control characters before I/O."""
    evidence = request.evidence
    values = (evidence.core_token, evidence.device_id, evidence.device, request.request_id, request.path)
    if any(not isinstance(value, str) or not value or "\r" in value or "\n" in value for value in values):
        raise ValueError("invalid relay evidence")
    headers = [
        ("Host", f"{host}:{port}"),
        ("Accept", "application/json"),
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("X-AX-Gate-Token", evidence.core_token),
        ("X-AX-Device-Id", evidence.device_id),
        ("X-AX-Device", evidence.device),
        ("X-AX-Request-Id", request.request_id),
    ]
    if evidence.bearer:
        if "\r" in evidence.bearer or "\n" in evidence.bearer:
            raise ValueError("invalid bearer")
        headers.append(("Authorization", "Bearer " + evidence.bearer))
    for name, header in _DECLARED_HEADERS.items():
        value = request.declared.get(name)
        if value:
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise ValueError("invalid declared value")
            headers.append((header, value))
    return headers


class HttpRelay:
    """A dependency-free, one-request-per-connection HTTP client for intake."""
    def __init__(self, host: str = "intake", port: int = 8082):
        self.host, self.port = host, port

    async def __call__(self, request: RelayRequest, timeout: float) -> Reply:
        if not request.path.startswith("/") or "\r" in request.path or "\n" in request.path:
            raise ValueError("invalid relay path")
        body = b"" if request.body is None else json.dumps(request.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        async def exchange() -> Reply:
            writer = None
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                lines = [f"{request.method} {request.path} HTTP/1.1"]
                lines.extend(f"{name}: {value}" for name, value in wire_headers(request, body, host=self.host, port=self.port))
                writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body)
                await writer.drain()
                return await self._read_reply(reader)
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass

        try:
            return await asyncio.wait_for(exchange(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("relay timeout") from exc

    @staticmethod
    async def _read_reply(reader: asyncio.StreamReader) -> Reply:
        status_line = await reader.readline()
        try:
            _version, status, _reason = status_line.decode("ascii").rstrip("\r\n").split(" ", 2)
            status_i = int(status)
        except (UnicodeDecodeError, ValueError):
            raise ValueError("invalid relay response") from None
        headers: dict[str, str] = {}
        for _ in range(64):
            line = await reader.readline()
            if line in (b"\r\n", b"\n"):
                break
            try:
                name, value = line.decode("latin-1").rstrip("\r\n").split(":", 1)
            except (UnicodeDecodeError, ValueError):
                raise ValueError("invalid relay response") from None
            name = name.lower()
            if name in headers:
                raise ValueError("duplicate relay response header")
            headers[name] = value.strip()
        else:
            raise ValueError("too many relay response headers")
        try:
            size = int(headers.get("content-length", "0"))
        except ValueError:
            raise ValueError("invalid relay response length") from None
        if size < 0 or size > _MAX_RESPONSE_BYTES:
            raise ValueError("relay response too large")
        raw = await reader.readexactly(size) if size else b""
        try:
            data = json.loads(raw) if raw else None
        except (UnicodeDecodeError, ValueError):
            raise ValueError("invalid relay response JSON") from None
        return Reply(status_i, data, headers)


async def transport(request: RelayRequest, timeout: float) -> Reply:
    """Compatibility entry point for callers without an explicit Settings object."""
    return await HttpRelay()(request, timeout)


__all__ = ["HttpRelay", "RelayEvidence", "RelayRequest", "Reply", "Transport", "transport", "unresolved_transport", "wire_headers"]
