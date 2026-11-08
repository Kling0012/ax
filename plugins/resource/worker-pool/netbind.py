"""2 つの口を、それぞれの網のアドレスだけに結び付ける（F-18.4）。timeline の netbind と同じ。"""
from __future__ import annotations

import ipaddress
import os
import socket
import struct

CORE_SUBNET = "192.0.2.0/25"
WORK_SUBNET = "198.51.100.0/25"


def local_ipv4_addresses() -> list[str]:
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        return []
    out: list[str] = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _idx, name in socket.if_nameindex():
            try:
                packed = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
                out.append(socket.inet_ntoa(packed[20:24]))
            except OSError:
                continue
    finally:
        s.close()
    return out


def pick_in_subnet(addresses: list[str], subnet: str) -> list[str]:
    try:
        net = ipaddress.ip_network(subnet)
        return [a for a in addresses if ipaddress.ip_address(a) in net]
    except ValueError:
        import logging
        logging.getLogger(__name__).error("Invalid subnet or address in network bind configuration")
        return []


def resolve_binds(environ: dict | None = None, addresses: list[str] | None = None) -> tuple[list[str], list[str]]:
    env = os.environ if environ is None else environ
    addrs = addresses if addresses is not None else local_ipv4_addresses()
    core = [a for a in (env.get("AX_WORKER_POOL_CORE_BIND") or "").split(",") if a] or pick_in_subnet(
        addrs, env.get("AX_CORE_SUBNET", CORE_SUBNET)
    )
    work = [a for a in (env.get("AX_WORKER_POOL_WORK_BIND") or "").split(",") if a] or pick_in_subnet(
        addrs, env.get("AX_WORK_SUBNET", WORK_SUBNET)
    )
    if "127.0.0.1" not in core:
        core = core + ["127.0.0.1"]
    return core, work


def bind_sockets(hosts: list[str], port: int) -> list[socket.socket]:
    socks = []
    for h in hosts:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((h, port))
        s.set_inheritable(True)
        socks.append(s)
    return socks
