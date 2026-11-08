"""egress-proxy の試験用の偽のストリームと偽の名前解決・接続（ネットワークを使わない）。"""
from __future__ import annotations

import asyncio


class FakeWriter:
    """asyncio.StreamWriter の最小の代わり。書かれた物を data に溜める。"""

    def __init__(self):
        self.data = b""
        self.closed = False
        self.eof_written = False

    def write(self, b: bytes) -> None:
        self.data += b

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        self.eof_written = True

    def get_extra_info(self, name, default=None):
        return default


def reader_with(data: bytes, eof: bool = True) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    if eof:
        r.feed_eof()
    return r


class FakeNet:
    """名前解決の表と、接続先ごとの上流の応答。接続の記録を持つ。"""

    def __init__(self, dns: dict | None = None, upstream: dict | None = None):
        self.dns = dns or {}
        self.upstream = upstream or {}  # (addr, port) -> 上流が返す bytes
        self.resolved: list = []
        self.connected: list = []
        self.writers: dict = {}

    async def resolver(self, host, port):
        self.resolved.append((host, port))
        if host not in self.dns:
            raise OSError("名前解決できない")
        return list(self.dns[host])

    async def connector(self, host, port):
        self.connected.append((host, port))
        if (host, port) not in self.upstream:
            raise OSError("接続できない")
        w = FakeWriter()
        self.writers[(host, port)] = w
        return reader_with(self.upstream[(host, port)]), w
