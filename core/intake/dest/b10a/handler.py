"""入口の行き先 `emergency` のハンドラ（S-12.3、S-6.4、S-6.11）。入口のプロセスの中で動く。

- `from=hq` だけを通す（登録簿の allowed_from でも絞る。二重の防御）。承認（S-6.10。tier は irreversible）は入口の検証が先に行う。
- 範囲 scope を確かめ、`emergency` サービス（ax-core）の POST /v1/emergency へ渡す。入口の資格は secrets の core-token-intake
  （環境変数 AX_CORE_TOKEN_FILE で場所を替えられる）。
- 範囲 `commanders` は lockdown（S-6.11）。入口の状態（席、保持、承認待ち）を触るので入口の中で行う:
  `app.state.intake.lockdown`（b12b1〜b12b3 が差し込む呼び出し）を呼ぶ。無ければ lockdown だけ行われなかったことを結果に書く
  （NotImplementedError 相当。OPEN.md の b10a）。そのうえで、emergency サービスへ渡して commander の命令を解除する。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from axcommon import creds, schemas
from axcommon.client import ApiClient, ApiError, Unavailable, service_url

log = logging.getLogger("axintake.dest.b10a")
TIMEOUT_S = 120.0   # スナップショットの記録に時間がかかりうる


def _err(status: int, code: str, message: str) -> tuple[int, dict]:
    return status, {"error": {"code": code, "message": message}}


def _token() -> str | None:
    path = os.environ.get("AX_CORE_TOKEN_FILE")
    if path:
        try:
            from pathlib import Path

            return Path(path).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return creds.read_secret_optional("core-token-intake")


def emergency_handler(app: Any, dest: dict, body: dict, ident: Any) -> tuple[int, Any]:
    if getattr(ident, "from_", None) != "hq" or getattr(ident, "via", None) != "hq":
        return _err(403, "not_allowed_from", "緊急ボタンは HQ だけ（S-6.4）")
    payload = body.get("payload") or {}
    if errs := schemas.errors("ext/b10a/emergency.payload.schema.json", payload):
        return _err(422, "payload_schema", "; ".join(errs[:3]))
    scope = payload["scope"]
    st = app.state.intake
    lockdown: dict[str, Any] | None = None
    if scope == "commanders":
        fn = getattr(st, "lockdown", None)
        if callable(fn):
            try:
                fn(True, ident)
                lockdown = {"done": True}
            except Exception as e:  # noqa: BLE001  lockdown の失敗で、命令の解除まで止めない
                log.error("lockdown の失敗: %s", type(e).__name__)
                lockdown = {"done": False, "error": type(e).__name__}
        else:
            lockdown = {"done": False, "error": "lockdown_not_implemented"}
    base = os.environ.get("AX_EMERGENCY_URL") or service_url("emergency", environ=dict(os.environ))
    client = ApiClient(base, token=_token(), transport=getattr(st, "transport", None), timeout=TIMEOUT_S)
    request = {"scope": scope}
    if body.get("idempotency_key"):
        key = body["idempotency_key"]
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            return _err(422, "bad_request", "idempotency_key は 1〜200 文字")
        request["request_id"] = key
    try:
        result = client.post("/v1/emergency", request)
    except Unavailable:
        # 緊急は止める向き。届かないことは記録して上へ出す（入口が instruction.rejected を書く）
        raise
    except ApiError as e:
        return e.status, (e.body if isinstance(e.body, dict) and e.body else {"error": {"code": e.code, "message": e.message}})
    out = dict(result) if isinstance(result, dict) else {"result": result}
    if lockdown is not None:
        out["lockdown"] = lockdown
    return 200, out
