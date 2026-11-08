"""HQ health probe through its private socket; no TCP fallback."""
from __future__ import annotations

import json
from .notify.intake import _UnixConnection


def main():
    connection = _UnixConnection("/run/hq/hq.sock", 3.0)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        body = json.loads(response.read(65536))
        return 0 if response.status == 200 and body.get("ok") is True and not body.get("config_unavailable") else 1
    except Exception:
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
