"""Linux の接続元 uid（D-5.1、S-6.8）。失敗を呼び出し元で拒否する。"""
from __future__ import annotations

import socket
import struct


def peer_uid(conn: socket.socket) -> int:
    """接続から毎回 SO_PEERCRED を読む。申告ヘッダは参照しない。"""
    if conn.family != socket.AF_UNIX:
        raise ValueError("peer_uid requires AF_UNIX")
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    if uid < 0:
        raise ValueError("invalid peer uid")
    return uid
