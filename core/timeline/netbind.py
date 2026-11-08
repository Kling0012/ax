"""2 つの口を、それぞれの網のアドレスだけに結び付ける（F-18.4）。

timeline は ax-core と ax-work の両方に入る。8080 を全アドレスで待ち受けると ax-work 側からも届いてしまうため、
8080 は ax-core のアドレス（と healthcheck 用の 127.0.0.1）、8081 は ax-work のアドレスだけに結び付ける。
アドレスは、自分のインターフェースのアドレスのうち、網の範囲（F-18.1）に入る物を選ぶ。環境変数で上書きできる。
  AX_CORE_SUBNET（既定 192.0.2.0/25）、AX_WORK_SUBNET（既定 198.51.100.0/25）
  AX_TIMELINE_CORE_BIND / AX_TIMELINE_WORK_BIND  カンマ区切りのアドレス（上書き。試験と単体起動用）

core_only_binds は ax-core だけに入る部品用の共通の結び付け先。0.0.0.0 で待ち受けず、
自分の ax-core のアドレスと 127.0.0.1 だけに結び付ける（D-14）。環境変数の名前を引数で渡し、
各部品は AX_<部品>_BIND（カンマ区切り）で上書きできる（既定は網のアドレスの自動選択）。
"""
from __future__ import annotations

import ipaddress
import os
import socket
import struct

CORE_SUBNET = "192.0.2.0/25"
WORK_SUBNET = "198.51.100.0/25"


def local_ipv4_addresses() -> list[str]:
    """Linux のインターフェースごとの IPv4 アドレス（SIOCGIFADDR）。取れなければ空。"""
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
    """(8080 の結び付け先, 8081 の結び付け先) を返す。8080 には healthcheck 用の 127.0.0.1 を足す。
    ax-work 側のアドレスが見つからないときは 8081 を開かない（空）。ax-core 側が見つからないときは 127.0.0.1 だけ。"""
    env = os.environ if environ is None else environ
    addrs = addresses if addresses is not None else local_ipv4_addresses()
    core = [a for a in (env.get("AX_TIMELINE_CORE_BIND") or "").split(",") if a] or pick_in_subnet(addrs, env.get("AX_CORE_SUBNET", CORE_SUBNET))
    work = [a for a in (env.get("AX_TIMELINE_WORK_BIND") or "").split(",") if a] or pick_in_subnet(addrs, env.get("AX_WORK_SUBNET", WORK_SUBNET))
    if "127.0.0.1" not in core:
        core = core + ["127.0.0.1"]
    return core, work


def core_only_binds(bind_var: str, environ: dict | None = None, addresses: list[str] | None = None) -> list[str]:
    """ax-core のアドレスと 127.0.0.1 だけを返す（D-14）。

    bind_var の環境変数（カンマ区切り）があればそれを結び付け先にする。無ければ自分のインターフェースの
    うち ax-core の範囲に入る物を選ぶ。ax-core のアドレスが見つからないときは 127.0.0.1 だけにする
    （空では無く、healthcheck は通る。網の外へは開けない）。0.0.0.0 は決して返さない。
    """
    env = os.environ if environ is None else environ
    addrs = addresses if addresses is not None else local_ipv4_addresses()
    core = [a for a in (env.get(bind_var) or "").split(",") if a] or pick_in_subnet(addrs, env.get("AX_CORE_SUBNET", CORE_SUBNET))
    if "0.0.0.0" in core:
        core = [a for a in core if a != "0.0.0.0"]
    if "127.0.0.1" not in core:
        core = core + ["127.0.0.1"]
    return core


def bind_sockets(hosts: list[str], port: int) -> list[socket.socket]:
    socks = []
    for h in hosts:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((h, port))
        s.set_inheritable(True)
        socks.append(s)
    return socks
