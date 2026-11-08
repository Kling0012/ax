"""Unix-only HQ runtime; importing this module opens no listener."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import stat


def validate_socket_directory(path: Path, expected_uid: int):
    parent = path.parent
    info = parent.lstat()
    if (parent.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != expected_uid
            or os.getuid() != expected_uid or stat.S_IMODE(info.st_mode) != 0o700):
        raise PermissionError("hq_socket_directory_unsafe")
    if path.exists() or path.is_symlink():
        raise FileExistsError("hq_socket_exists_archive_before_restart")


def serve(app, path: Path, *, expected_uid: int):
    import uvicorn

    validate_socket_directory(path, expected_uid)
    os.umask(0o177)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(2048)
        config = uvicorn.Config(app, proxy_headers=False, access_log=False, server_header=False,
                                log_level="warning", lifespan="on")
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        listener.close()
    # Keep the socket for the host's explicit archive/restart procedure.


def start_receipt_listener(app):
    """Run the isolated component API without affecting the HQ socket."""
    import logging
    import threading
    import uvicorn

    def run():
        try:
            config = uvicorn.Config(app, host="0.0.0.0", port=8083, proxy_headers=False,
                                    access_log=False, server_header=False, log_level="warning", lifespan="off")
            uvicorn.Server(config).run()
        except BaseException:
            logging.getLogger("hq.receipts").error("receipt listener unavailable")
    thread = threading.Thread(target=run, name="hq-receipts", daemon=True)
    thread.start()
    return thread
