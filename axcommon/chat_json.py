"""chat-json の runner のクライアント（F-7.2、F-19.1、D-7.2）。ジョブ用コンテナ（ax-job）で 1 回ごとに動く。

起動: `python -m axcommon.chat_json`（config/runners/chat-json.yaml の command）。
入力  ${AX_CHAT_JSON_DIR:-/work}/request.json  worker-pool が書く（model、max_tokens、system、messages、任意の schema）。
出力  ${AX_CHAT_JSON_DIR:-/work}/response.json  応答の本文とトークン数（schemas/ext/b6b/chat-json-response.schema.json）。
モデルの入口は環境変数 ANTHROPIC_BASE_URL（launcher が http://egress-proxy:3425 に固定）と ANTHROPIC_AUTH_TOKEN の 2 つだけを読む。
profile の model_gateway を直接呼ばない。鍵は出力にもログにも出さない。
終了コード: 0 成功 / 2 request.json の不備 / 3 ゲートウェイの失敗（rate_limited・quota・unauthorized・server_error・unavailable・gateway） /
  4 応答の本文が JSON でない / 5 schema に合わない（呼び出し側は bug として扱う。D-6.3）。
利用制限（HTTP 429 や quota）は能力上の失敗に数えない（R-N22）。error.class で呼び出し側が見分ける。
標準ライブラリ中心。jsonschema は schema があるときだけ import する。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from .client import Unavailable, urllib_transport

TIMEOUT_S = 600.0
ANTHROPIC_VERSION = "2023-06-01"
Transport = Callable[[str, str, dict, "bytes | None", float], "tuple[int, dict, bytes]"]


def _write_response(work: Path, obj: dict) -> None:
    tmp = work / ".response.json.tmp"
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, work / "response.json")


def _fail(work: Path, code: int, cls: str, message: str, status: int | None = None, extra: dict | None = None) -> int:
    err: dict[str, Any] = {"class": cls, "message": message}
    if status is not None:
        err["status"] = status
    if extra:
        err.update(extra)
    _write_response(work, {"ok": False, "exit_code": code, "error": err})
    return code


def classify_http(status: int, body: bytes) -> str:
    text = body[:2000].decode("utf-8", "replace").lower()
    if status == 429:
        return "quota" if any(w in text for w in ("quota", "insufficient", "billing", "exceeded your")) else "rate_limited"
    if status in (401, 403):
        return "unauthorized"
    if status >= 500:
        return "server_error"
    return "gateway"


def extract_text(resp: dict) -> str:
    parts = []
    for block in resp.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def parse_json_text(text: str) -> tuple[bool, Any]:
    """本文を JSON として読む。```json の囲みがあれば外して読む。"""
    s = text.strip()
    if s.startswith("```"):
        lines = s.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    try:
        return True, json.loads(s)
    except ValueError:
        return False, None


def run(work: Path, environ: dict, transport: Transport | None = None) -> int:
    try:
        req = json.loads((work / "request.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _fail(work, 2, "bad_request", "request.json が読めない")
    errs = _check_request(req)  # schemas/ext/b6b/chat-json-request.schema.json と同じ検査（コンテナの中は標準ライブラリだけで動かす）
    if errs:
        return _fail(work, 2, "bad_request", "; ".join(errs[:5]))
    base = environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    token = environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not base.startswith(("http://", "https://")) or not token:
        return _fail(work, 3, "gateway", "ANTHROPIC_BASE_URL と ANTHROPIC_AUTH_TOKEN が要る")
    body = {k: req[k] for k in ("model", "max_tokens", "system", "messages", "temperature") if k in req}
    headers = {"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION, "authorization": "Bearer " + token}
    send = transport or urllib_transport
    try:
        status, _hdr, raw = send("POST", base + "/v1/messages", headers, json.dumps(body).encode("utf-8"), TIMEOUT_S)
    except Unavailable as e:
        return _fail(work, 3, "unavailable", f"ゲートウェイに届かない: {e}")
    if status != 200:
        return _fail(work, 3, classify_http(status, raw), f"HTTP {status}", status=status)
    try:
        resp = json.loads(raw.decode("utf-8"))
        if not isinstance(resp, dict):
            raise ValueError
    except ValueError:
        return _fail(work, 3, "gateway", "応答が JSON でない", status=status)
    text = extract_text(resp)
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    base_out: dict[str, Any] = {"text": text, "usage": {k: v for k, v in usage.items() if isinstance(v, int)},
                                "model": resp.get("model") if isinstance(resp.get("model"), str) else req["model"], "stop_reason": resp.get("stop_reason")}
    ok, value = parse_json_text(text)
    if not ok:
        _write_response(work, {"ok": False, "exit_code": 4, **base_out, "error": {"class": "not_json", "status": status, "message": "応答の本文が JSON でない"}})
        return 4
    schema = req.get("schema")
    if schema is not None:
        try:
            import jsonschema

            v = jsonschema.Draft202012Validator(schema)
            serrs = sorted(f"{'/'.join(str(p) for p in e.absolute_path) or '/'}: {e.message}" for e in v.iter_errors(value))
        except Exception as e:  # noqa: BLE001  schema 自体が不正
            serrs = [f"schema を使えない: {type(e).__name__}"]
        if serrs:
            _write_response(work, {"ok": False, "exit_code": 5, **base_out, "json": value,
                                   "error": {"class": "schema_mismatch", "message": "schema に合わない", "schema_errors": serrs[:20]}})
            return 5
    _write_response(work, {"ok": True, "exit_code": 0, **base_out, "json": value})
    return 0


def _check_request(req: Any) -> list[str]:
    """schemas/ext/b6b/chat-json-request.schema.json の最小の代わり（jsonschema が無いとき）。"""
    if not isinstance(req, dict):
        return ["request.json がオブジェクトでない"]
    errs = []
    allowed = {"model", "max_tokens", "system", "messages", "schema", "temperature"}
    errs += [f"未知の項目: {k}" for k in req if k not in allowed]
    if not isinstance(req.get("model"), str) or not req.get("model"):
        errs.append("model が要る")
    if not isinstance(req.get("max_tokens"), int) or isinstance(req.get("max_tokens"), bool) or req["max_tokens"] < 1:
        errs.append("max_tokens は 1 以上の整数")
    m = req.get("messages")
    if not isinstance(m, list) or not m or not all(isinstance(x, dict) and "role" in x and "content" in x for x in m):
        errs.append("messages は role と content を持つ配列")
    if "system" in req and not isinstance(req["system"], (str, list)):
        errs.append("system は文字列か配列")
    if "schema" in req and not isinstance(req["schema"], dict):
        errs.append("schema はオブジェクト")
    return errs


def main(argv: list[str] | None = None) -> int:
    work = Path(os.environ.get("AX_CHAT_JSON_DIR", "/work"))
    try:
        return run(work, dict(os.environ))
    except OSError as e:
        print(f"chat_json: 書き込めない: {e.strerror}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    sys.exit(main())
