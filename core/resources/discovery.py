"""提供一覧の取得と登録簿との照合（S-8.2、R-N28、R-N29、F-7.1）。

- ゲートウェイの GET /v1/models を呼ぶ（資格は profile の model_gateway。秘密の名前は
  model-gateway-key。値は書かない）。Advertise されている model id は provider/model の形。
- 取得時刻、成功・失敗、新規・一致・未提供の ID と失敗理由を記録する。取得失敗を空の成功一覧とせず、
  照合だけで登録状態を有効化しない（S-8.2）。
- group/* や auto/* のエントリは選ばないために登録しない（F-7.3）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from axcommon import creds, timeutil
from axcommon.client import ApiClient, ApiError, Unavailable

from .store import Store

def gateway_url(env: dict | None = None) -> str:
    e = os.environ if env is None else env
    if e.get("AX_MODEL_GATEWAY"):
        return e["AX_MODEL_GATEWAY"]
    raise ValueError("missing_config:AX_MODEL_GATEWAY")


def _token(secrets_base: str | os.PathLike | None = None) -> str | None:
    try:
        return creds.read_secret("model-gateway-key", base=secrets_base)
    except (creds.SecretMissing, UnicodeError):
        return None


def fetch_models(*, client: ApiClient | None = None, transport=None, token: str | None = None,
                 base_url: str | None = None) -> dict:
    """提供一覧を取得する。成功なら {"ok": True, "provided": [...]}、失敗なら {"ok": False, "error": ...}。"""
    if client is None and token is None:
        token = _token()
        if not token:
            return {"ok": False, "error": "model_gateway_key_unavailable"}
    try:
        c = client or ApiClient(base_url or gateway_url(), token=token, transport=transport)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        doc = c.get("/v1/models")
    except ApiError as e:
        return {"ok": False, "error": f"http_{e.status}"}
    except Unavailable as e:
        return {"ok": False, "error": f"unavailable:{type(e).__name__}"}
    data = (doc or {}).get("data") or []
    provided: list[str] = []
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        mid = entry["id"]
        if mid.startswith(("group/", "auto/")):
            continue  # AX が具体的なモデルを自分で選ぶ（F-7.3）
        provided.append(mid)
    return {"ok": True, "provided": provided}


def reconcile(store: Store, *, transport=None, token: str | None = None, base_url: str | None = None) -> dict:
    """取得と照合を一度に行う。結果は discovery 表に残す（S-8.2）。"""
    now = timeutil.now_ts()
    res = fetch_models(transport=transport, token=token, base_url=base_url)
    if not res["ok"]:
        doc = store.record_discovery(ok=False, provided=[], error=res["error"], now=now)
        doc["fetched_at"] = now
        return doc
    doc = store.record_discovery(ok=True, provided=res["provided"], now=now)
    doc["fetched_at"] = now
    return doc
