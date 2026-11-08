"""`ax claim acquire|renew|release <target_key>` (S-6.17, S-24.1). Standard library only.

The entry wraps the destination's answer in `result`. S-24.1 tools read only
`data` of `ax claim ... --json`, so `normalize` lifts `result` into `data` and
maps a refused operation to `error.code` (`conflict` gives exit code 8).
"""
from __future__ import annotations

import re

DESTINATION = "claim.op"
OPS = ("acquire", "renew", "release")
TARGET_KEY = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")
SAY = {"acquire": "保持を取った。", "renew": "保持を延ばした。", "release": "保持を外した。"}


def build_request(arguments, global_args, new_id):
    """Return `(method, path, body, request_id)` for the entry's `POST /v1/instructions`."""
    arguments = list(arguments or [])
    if len(arguments) != 2 or arguments[0] not in OPS:
        raise ValueError("引数を確認する。ax claim acquire|renew|release <target_key>")
    op, target_key = arguments
    if not TARGET_KEY.fullmatch(target_key):
        raise ValueError("target_key は英数字と : . _ - の1〜128文字にする")
    request_id = global_args.request_id or new_id()
    if not request_id or len(request_id) > 200:
        raise ValueError("request-id は1〜200文字にする")
    body = {"from": "commander", "destination": DESTINATION,
            "payload": {"op": op, "target_key": target_key}, "idempotency_key": request_id}
    if global_args.session:
        body["session"] = global_args.session
    return "POST", "/v1/instructions", body, request_id


def normalize(status, response, request_id):
    """Lift the destination result into the `ax` envelope; other answers pass through unchanged."""
    if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
        return response
    result = response["result"]
    if 200 <= status < 300 and response.get("ok") is not False and "error" not in result:
        return {"ok": True, "op": "claim", "request_id": request_id,
                "say": SAY.get(result.get("op"), "入口が応答した。"), "data": result, "next": []}
    error = result.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return {"ok": False, "op": "claim", "request_id": request_id,
            "error": {"code": code or "internal", "retryable": code == "unavailable"},
            "say": "入口が保持の操作を拒否した。", "hint": "data.owner と data.lease_until を確認する。",
            "data": data}
