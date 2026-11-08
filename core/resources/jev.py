"""Jev（機能の資格）の呼び出し（S-8.4、F-7.4、R-N17）。

実体は TypeSafe System One のモデル jev-1.13.0。接続先は S-16.6 の「Jev」の行に登録された
接続先（F-7.4。ここでは環境変数 AX_JEV_URL）。応答時間は約 0.3 秒、タイムアウトは 20 秒。
資格は ${secrets_dir}/jev-key だけ（値は書かない）。

- 要求: POST の本文は {"model": "jev-1.13.0", "state": {…}, "questions": {"<名前>": {"type": "noul", "instructions": "<問い>"}}}。
  state に判定の材料（モデルの説明と作業の適合など）を入れる。
- 応答: {"answers": {"<名前>": {"noul": <0〜1の数>}}}。すべての問いに 0〜1 の数があるときだけ採用する。
  真偽値、範囲外、欠落、HTTP エラー、タイムアウトは「使えない」とする。
- 閾値: 「是」は 0.75 以上、「否」は 0.25 未満、その間は不明（旧設定の値。S-17 の既定に写して点検で見直す）。
- 学習利用の制限は設けない（利用者の決定）。送る内容から秘密と個人情報は除く。呼び出し側は
  秘密と個人情報の除いた材料（state）を渡す責任を持つ。Jev はこの関数が見た state の中身をログに出さない。
- ログにはステータス（HTTP コード）か例外の型名だけ書く。本文とヘッダーは書かない。

試験は偽の transport で行う（tests/fakes/b6a/jev_fake.py）。ネットワークは使わない。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("resources.jev")

MODEL = "jev-1.13.0"
DEFAULT_TIMEOUT_S = 20.0
YES_THRESHOLD = 0.75
NO_THRESHOLD = 0.25


def jev_url(env: dict | None = None) -> str:
    e = os.environ if env is None else env
    return e.get("AX_JEV_URL", "https://api.typesafe.ai/v1/systemone")


def jev_timeout(env: dict | None = None) -> float:
    e = os.environ if env is None else env
    try:
        return float(e.get("AX_JEV_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _read_key(base: str | os.PathLike | None = None) -> str | None:
    try:
        from axcommon import creds

        return creds.read_secret("jev-key", base=base)
    except Exception:  # noqa: BLE001  鍵が無くてもこの部品は止めない（fail-soft）
        return None


def _urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() or b""
    except OSError as e:
        raise TimeoutError(type(e).__name__) from e


def call(*, state: dict, questions: dict[str, str], transport: Callable | None = None, key: str | None = None,
         base: str | os.PathLike | None = None, env: dict | None = None) -> dict:
    """Jev を 1 回呼ぶ。結果は {"ok": bool, "answers": {名前: 数}, "raw": 数一覧か None, "reason": str}。
    使えない場合（無効、HTTP エラー、タイムアウト）は ok=False で返し、例外を出さない（S-8.4 の最安の規則）。"""
    k = key or _read_key(base)
    if k is None:
        return {"ok": False, "answers": {}, "reason": "key_missing"}
    body_obj = {"model": MODEL, "state": state,
                "questions": {n: {"type": "noul", "instructions": q} for n, q in questions.items()}}
    raw = json.dumps(body_obj).encode("utf-8")
    headers = {"Authorization": f"Bearer {k}", "Content-Type": "application/json"}
    tx = transport or _urllib_transport
    try:
        status, _h, out = tx("POST", jev_url(env), headers, raw, jev_timeout(env))
    except Exception as e:  # noqa: BLE001  ここだけ記録して下の最安の規則に渡す
        log.info("jev_unavailable %s", type(e).__name__)
        return {"ok": False, "answers": {}, "reason": f"transport:{type(e).__name__}"}
    if status != 200:
        log.info("jev_http %s", status)
        return {"ok": False, "answers": {}, "reason": f"http_{status}"}
    try:
        doc = json.loads((out or b"").decode("utf-8"))
    except ValueError:
        return {"ok": False, "answers": {}, "reason": "bad_json"}
    answers = (doc or {}).get("answers") or {}
    vals: dict[str, float] = {}
    for name in questions:
        a = answers.get(name) or {}
        v = a.get("noul") if isinstance(a, dict) else a
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0.0 <= v <= 1.0):
            return {"ok": False, "answers": {}, "reason": "bad_answer"}
        vals[name] = float(v)
    return {"ok": True, "answers": vals, "reason": "ok"}


def verdict(score: float | None) -> str:
    """「是」「否」「不明」（F-7.4）。"""
    if score is None:
        return "unknown"
    if score >= YES_THRESHOLD:
        return "yes"
    if score < NO_THRESHOLD:
        return "no"
    return "unknown"


def suitable_scores(scores: dict[str, float], *, yes_threshold: float = YES_THRESHOLD) -> set[str]:
    """適合と判定した候補の id の集合。"""
    return {mid for mid, v in scores.items() if v >= yes_threshold}
