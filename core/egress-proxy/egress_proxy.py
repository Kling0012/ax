"""egress-proxy（B6。F-18.2、F-18.4、S-8.11）。ジョブ用コンテナ（ax-job）の外向きの通信を許可先だけに絞る小さな asyncio の部品。

口は 3 つ（すべて 0.0.0.0 ではなくコンテナの網の中で待つ。compose の ports は持たない）:
  3128  HTTP プロキシ。
        - CONNECT: ユーザー名 job-<id>（Proxy-Authorization の Basic）に対応する許可ファイルのホスト名の 443 だけを通す。他は 403。
        - 素の HTTP の絶対 URI（CONNECT でない）: 許可ファイルにゲートの行 `@gate <port>` があるジョブの、ゲート（EGRESS_GATE_HOST:<port>）への中継だけ。
          宛先はこの 1 件だけ。行が無いジョブと他の宛先は 403（T-127 の境界。実測は B18 の T-182）。
  3425  モデルの入口（EGRESS_MODEL_GATEWAY の URL のホストとポート）への TCP 中継。常に許可。
  8080  GET /healthz（コンテナの healthcheck）。
許可ファイル: ${EGRESS_ALLOW_DIR}/job-<id>.allow（1 行 1 ホスト名。launcher が書く。読み取り専用で見る）。無い、読めない、ジョブ ID が不明なら
3425 以外は全拒否。ログはホスト名とジョブ ID だけ（URL、ヘッダー、本文、資格を出さない）。
CONNECT の宛先は名前解決の結果が公開アドレスのときだけ通す（プライベート、ループバック、リンクローカル、tailnet（100.64.0.0/10）は拒否。DNS の書き換えで
内側へ向けられるのを防ぐ）。解決したアドレスへ直接つなぎ、名前で再接続しない。
標準ライブラリだけ。環境変数: EGRESS_ALLOW_DIR=/egress、EGRESS_MODEL_GATEWAY（URL）、EGRESS_GATE_HOST、EGRESS_GATE_PORT、EGRESS_LISTEN_HOST=0.0.0.0、
EGRESS_PROXY_PORT=3128、EGRESS_GATEWAY_PORT=3425、AX_HEALTH_PORT=8080、EGRESS_CONNECT_TIMEOUT_S=10、EGRESS_IDLE_TIMEOUT_S=600、EGRESS_MAX_CONNS=256。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import os
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

VERSION = "0.1.0"
COMPONENT = "egress-proxy"
USER_RE = re.compile(r"^job-([a-z0-9-]{1,40})$")
HOST_RE = re.compile(r"^[a-z0-9.-]+$")
GATE_LINE_RE = re.compile(r"^@gate[ \t]+([0-9]{1,5})$")
MAX_HEADER_BYTES = 16 * 1024
HEADER_TIMEOUT_S = 10.0
BUF = 64 * 1024

Resolver = Callable[[str, int], Awaitable[list]]
Connector = Callable[[str, int], Awaitable[tuple]]


@dataclass
class AllowSet:
    hosts: frozenset
    gate_port: int | None = None


def read_allow(allow_dir: Path, job_id: str) -> AllowSet | None:
    """許可ファイルを読む。job_id の形が不正、ファイルが無い・読めないときは None（全拒否）。"""
    if not re.fullmatch(r"[a-z0-9-]{1,40}", job_id or ""):
        return None
    try:
        text = (Path(allow_dir) / f"job-{job_id}.allow").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    hosts, gate = set(), None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = GATE_LINE_RE.match(line)
        if m:
            p = int(m.group(1))
            if 1 <= p <= 65535:
                gate = p
            continue
        if line.startswith("@"):
            continue  # 知らない @ 行は読まない（allow_hosts のホスト名の行としても読まない）
        if HOST_RE.match(line) and len(line) <= 253:
            hosts.add(line)
    return AllowSet(frozenset(hosts), gate)


def read_fixed_allow(path: Path) -> AllowSet:
    """Load one immutable hostname policy; reject malformed or unreadable input."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("fixed allow file unavailable") from exc
    hosts = set()
    for raw in lines:
        host = raw.strip()
        if not host or host.startswith("#"):
            continue
        labels = host.split(".")
        if (len(host) > 253 or host != host.lower() or is_ip_literal(host)
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                       for label in labels)):
            raise ValueError("invalid fixed allow host")
        hosts.add(host)
    return AllowSet(frozenset(hosts))


def normalize_host(h: str) -> str:
    return h.strip().rstrip(".").lower()


def is_ip_literal(h: str) -> bool:
    try:
        ipaddress.ip_address(h.strip("[]"))
        return True
    except ValueError:
        return bool(re.fullmatch(r"[0-9.]+", h)) or ":" in h


def addr_ok(addr: str) -> bool:
    """CONNECT の宛先として許す解決結果か（公開アドレスだけ）。"""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def job_from_proxy_auth(value: str | None) -> str | None:
    """Proxy-Authorization: Basic base64(job-<id>:…) から job id を取り出す。パスワード部は見ない。"""
    if not value:
        return None
    parts = value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        raw = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    user = raw.split(":", 1)[0]
    m = USER_RE.match(user)
    return m.group(1) if m else None


@dataclass
class Context:
    allow_dir: Path
    gateway: tuple  # (host, port) モデルの入口
    gate_host: str | None = None
    gate_port: int | None = None
    resolver: Resolver | None = None
    connector: Connector | None = None
    connect_timeout_s: float = 10.0
    idle_timeout_s: float = 600.0
    log: Callable[[dict], None] = field(default=lambda rec: None)
    max_conns: int = 256
    _sem: asyncio.Semaphore | None = None
    fixed_allow: AllowSet | None = None

    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_conns)
        return self._sem

    async def resolve(self, host: str, port: int) -> list:
        if self.resolver:
            return await self.resolver(host, port)
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(i[4][0] for i in infos))

    async def connect(self, host: str, port: int):
        if self.connector:
            return await self.connector(host, port)
        return await asyncio.open_connection(host, port)


def log_event(ctx: Context, event: str, job: str | None, host: str | None, reason: str | None = None) -> None:
    rec = {"ts": round(time.time(), 3), "event": event, "job": job, "host": host}
    if reason:
        rec["reason"] = reason
    try:
        ctx.log(rec)
    except Exception:  # noqa: BLE001  ログの失敗で通信を止めない
        pass


async def read_head(reader: asyncio.StreamReader) -> tuple[str, dict, bytes] | None:
    """要求行とヘッダーを読む。(要求行, ヘッダー(小文字キー)、ヘッダーの後に読み過ぎた分) / 不正なら None。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > MAX_HEADER_BYTES:
            return None
        chunk = await asyncio.wait_for(reader.read(4096), HEADER_TIMEOUT_S)
        if not chunk:
            return None
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    try:
        lines = head.decode("latin-1").split("\r\n")
    except UnicodeDecodeError:
        return None
    request_line, hdr_lines = lines[0], lines[1:]
    headers: dict = {}
    for ln in hdr_lines:
        k, sep, v = ln.partition(":")
        if not sep:
            return None
        headers.setdefault(k.strip().lower(), []).append(v.strip())
    return request_line, headers, rest


async def respond(writer, status: int, reason: str, extra: str = "") -> None:
    body = f"{status} {reason}\n".encode()
    writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n{extra}\r\n".encode() + body)
    try:
        await writer.drain()
    except Exception:  # noqa: BLE001
        pass


async def _close(writer) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        pass


async def pipe(reader, writer, idle_s: float) -> None:
    try:
        while True:
            data = await asyncio.wait_for(reader.read(BUF), idle_s)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except Exception:  # noqa: BLE001
            pass


GRACE_S = 5.0


async def splice(c_reader, c_writer, u_reader, u_writer, idle_s: float) -> None:
    """双方向に中継する。サーバー側が閉じたら（u→c が終わる）、クライアント→サーバーに GRACE_S だけ待って打ち切る。
    クライアントが先に半閉じしたときは、サーバーの応答が終わるか idle_s の無通信まで待つ。"""
    t1 = asyncio.ensure_future(pipe(c_reader, u_writer, idle_s))
    t2 = asyncio.ensure_future(pipe(u_reader, c_writer, idle_s))
    try:
        await asyncio.wait({t2})
        if not t1.done():
            await asyncio.wait({t1}, timeout=GRACE_S)
    finally:
        for t in (t1, t2):
            if not t.done():
                t.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)


def split_hostport(target: str, default_port: int | None) -> tuple[str, int] | None:
    t = target.strip()
    if t.startswith("["):  # [v6]:port は IP リテラルなので呼び出し側が拒否する。形だけ読む
        host, _, port = t[1:].partition("]")
        port = port.lstrip(":")
    else:
        host, sep, port = t.rpartition(":")
        if not sep:
            host, port = t, ""
    if not host:
        return None
    if port == "":
        return (host, default_port) if default_port else None
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        return None
    return host, int(port)


async def handle_proxy(reader, writer, ctx: Context) -> None:
    """ポート 3128 の 1 接続。"""
    async with ctx.sem():
        try:
            head = await read_head(reader)
            if head is None:
                await respond(writer, 400, "Bad Request")
                return
            request_line, headers, rest = head
            parts = request_line.split(" ")
            if len(parts) != 3 or not parts[2].startswith("HTTP/"):
                await respond(writer, 400, "Bad Request")
                return
            method, target, _ = parts
            job = job_from_proxy_auth((headers.get("proxy-authorization") or [None])[0])
            allow = ctx.fixed_allow if ctx.fixed_allow is not None else (read_allow(ctx.allow_dir, job) if job else None)
            if method.upper() == "CONNECT":
                await handle_connect(reader, writer, ctx, job, allow, target, rest)
            else:
                await handle_plain(reader, writer, ctx, job, allow, method, target, headers, rest)
        except asyncio.TimeoutError:
            await respond(writer, 408, "Request Timeout")
        except (ConnectionError, OSError):
            pass
        finally:
            await _close(writer)


async def handle_connect(reader, writer, ctx, job, allow, target, rest) -> None:
    hp = split_hostport(target, None)
    if hp is None:
        await respond(writer, 400, "Bad Request")
        return
    host, port = hp
    host = normalize_host(host)
    if allow is None:
        log_event(ctx, "connect_deny", job, host, "no_allow_file")
        await respond(writer, 403, "Forbidden")
        return
    if is_ip_literal(host):
        log_event(ctx, "connect_deny", job, host, "ip_literal")
        await respond(writer, 403, "Forbidden")
        return
    if port != 443 or host not in allow.hosts:
        log_event(ctx, "connect_deny", job, host, "not_allowed" if port == 443 else "port")
        await respond(writer, 403, "Forbidden")
        return
    try:
        addrs = await asyncio.wait_for(ctx.resolve(host, port), ctx.connect_timeout_s)
    except (asyncio.TimeoutError, OSError):
        log_event(ctx, "connect_fail", job, host, "resolve")
        await respond(writer, 502, "Bad Gateway")
        return
    good = [a for a in addrs if addr_ok(a)]
    if not good or len(good) != len(addrs):
        log_event(ctx, "connect_deny", job, host, "non_public_address")
        await respond(writer, 403, "Forbidden")
        return
    up = None
    for a in good:
        try:
            up = await asyncio.wait_for(ctx.connect(a, port), ctx.connect_timeout_s)
            break
        except (asyncio.TimeoutError, OSError):
            continue
    if up is None:
        log_event(ctx, "connect_fail", job, host, "connect")
        await respond(writer, 502, "Bad Gateway")
        return
    u_reader, u_writer = up
    log_event(ctx, "connect_allow", job, host)
    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await writer.drain()
    if rest:
        u_writer.write(rest)
        await u_writer.drain()
    try:
        await splice(reader, writer, u_reader, u_writer, ctx.idle_timeout_s)
    finally:
        await _close(u_writer)


HOP_HEADERS = {"proxy-authorization", "proxy-connection", "connection", "keep-alive", "te", "trailer", "upgrade"}


async def handle_plain(reader, writer, ctx, job, allow, method, target, headers, rest) -> None:
    """素の HTTP の絶対 URI。ゲート（@gate の行があるジョブ → EGRESS_GATE_HOST:<port>）への中継だけ。"""
    if not target.lower().startswith("http://"):
        await respond(writer, 400, "Bad Request")  # プロキシ形式でない（origin-form など）
        return
    sp = urlsplit(target)
    host = normalize_host(sp.hostname or "")
    port = sp.port or 80
    if allow is None or allow.gate_port is None or not ctx.gate_host or ctx.gate_port is None:
        log_event(ctx, "http_deny", job, host, "no_gate_line" if allow is not None else "no_allow_file")
        await respond(writer, 403, "Forbidden")
        return
    if host != normalize_host(ctx.gate_host) or port != allow.gate_port or port != ctx.gate_port:
        log_event(ctx, "http_deny", job, host, "not_gate")
        await respond(writer, 403, "Forbidden")
        return
    try:
        u_reader, u_writer = await asyncio.wait_for(ctx.connect(ctx.gate_host, ctx.gate_port), ctx.connect_timeout_s)
    except (asyncio.TimeoutError, OSError):
        log_event(ctx, "http_fail", job, host, "connect")
        await respond(writer, 502, "Bad Gateway")
        return
    log_event(ctx, "http_allow", job, host)
    path = sp.path or "/"
    if sp.query:
        path += "?" + sp.query
    out = [f"{method} {path} HTTP/1.1", f"Host: {host}:{port}"]
    for k, vs in headers.items():
        if k in HOP_HEADERS or k == "host":
            continue
        for v in vs:
            out.append(f"{k}: {v}")
    out.append("Connection: close")  # 1 接続 1 要求。単純な中継にする
    u_writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + rest)
    await u_writer.drain()
    try:
        await splice(reader, writer, u_reader, u_writer, ctx.idle_timeout_s)
    finally:
        await _close(u_writer)


async def handle_gateway(reader, writer, ctx: Context) -> None:
    """ポート 3425: モデルの入口への TCP 中継（常に許可）。"""
    async with ctx.sem():
        try:
            host, port = ctx.gateway
            try:
                u_reader, u_writer = await asyncio.wait_for(ctx.connect(host, port), ctx.connect_timeout_s)
            except (asyncio.TimeoutError, OSError):
                log_event(ctx, "gateway_fail", None, host, "connect")
                return
            log_event(ctx, "gateway_relay", None, host)
            try:
                await splice(reader, writer, u_reader, u_writer, ctx.idle_timeout_s)
            finally:
                await _close(u_writer)
        except (ConnectionError, OSError):
            pass
        finally:
            await _close(writer)


async def handle_health(reader, writer) -> None:
    try:
        head = await read_head(reader)
        ok = head is not None and head[0].startswith("GET /healthz")
        body = json.dumps({"ok": True, "component": COMPONENT, "version": VERSION} if ok else {"error": {"code": "not_found", "message": ""}}).encode()
        writer.write(f"HTTP/1.1 {200 if ok else 404} {'OK' if ok else 'Not Found'}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        await _close(writer)


def parse_gateway(url: str) -> tuple[str, int]:
    sp = urlsplit(url)
    if sp.scheme not in ("http", "https") or not sp.hostname:
        raise ValueError("EGRESS_MODEL_GATEWAY は http(s) の URL")
    return sp.hostname, sp.port or (443 if sp.scheme == "https" else 80)


def context_from_env(environ: dict) -> Context:
    fixed_path = environ.get("EGRESS_FIXED_ALLOW_FILE")
    if "EGRESS_FIXED_ALLOW_FILE" in environ and not fixed_path:
        raise SystemExit("egress-proxy: EGRESS_FIXED_ALLOW_FILE is empty")
    try:
        fixed_allow = read_fixed_allow(Path(fixed_path)) if fixed_path else None
    except ValueError as exc:
        raise SystemExit("egress-proxy: invalid fixed allow file") from exc
    gw = environ.get("EGRESS_MODEL_GATEWAY", "")
    if not gw and fixed_allow is None:
        raise SystemExit("egress-proxy: EGRESS_MODEL_GATEWAY が無い")

    def log(rec: dict) -> None:
        sys.stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    gate_port = environ.get("EGRESS_GATE_PORT")
    return Context(
        allow_dir=Path(environ.get("EGRESS_ALLOW_DIR", "/egress")), gateway=parse_gateway(gw) if gw and fixed_allow is None else ("", 0),
        fixed_allow=fixed_allow,
        gate_host=environ.get("EGRESS_GATE_HOST") or None, gate_port=int(gate_port) if gate_port else None,
        connect_timeout_s=float(environ.get("EGRESS_CONNECT_TIMEOUT_S", "10")), idle_timeout_s=float(environ.get("EGRESS_IDLE_TIMEOUT_S", "600")),
        max_conns=int(environ.get("EGRESS_MAX_CONNS", "256")), log=log)


async def serve(ctx: Context, environ: dict) -> None:
    host = environ.get("EGRESS_LISTEN_HOST", "0.0.0.0")  # noqa: S104  コンテナの中。外へは ports を出さない（compose）
    servers = [await asyncio.start_server(lambda r, w: handle_proxy(r, w, ctx), host, int(environ.get("EGRESS_PROXY_PORT", "3128")))]
    if ctx.fixed_allow is None:
        servers.extend([
            await asyncio.start_server(lambda r, w: handle_gateway(r, w, ctx), host, int(environ.get("EGRESS_GATEWAY_PORT", "3425"))),
            await asyncio.start_server(handle_health, host, int(environ.get("AX_HEALTH_PORT", "8080"))),
        ])
    await asyncio.gather(*(s.serve_forever() for s in servers))


def main() -> int:
    ctx = context_from_env(dict(os.environ))
    try:
        asyncio.run(serve(ctx, dict(os.environ)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
