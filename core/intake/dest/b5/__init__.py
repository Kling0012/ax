"""b5 の行き先ハンドラ: `control.command` と `ledger.answer`（S-6.4、D-5 の表）。

- control.command: `issued_by` を受け口で決めて制御 API へ（hq の利用者本人なら `user`、司令塔の
  受け口なら `commander`。後者は期限が必須。S-5.3）。
- ledger.answer: 台帳の回答の口へ。司令塔の受け口からは `answer_source: relay` と利用者の発言の原文
  `quote` を添える（S-6.9）。`user_only` のカードの拒否（reason `user_only_card`）は台帳が同じ条件で
  行う（S-9.2）ので、ここでは台帳の応答をそのまま返す。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote as urlquote

from axcommon import schemas
from axcommon.client import ApiClient
from axcommon.creds import read_secret_optional

from core.intake.app import api_from_handler
from core.intake.registry import ts_expires_at
from core.intake.vias import Ident

CONTROL_FIELDS = ("target", "op", "args", "reason", "expires_at", "on_expiry", "corr")


def register(app) -> None:
    handlers = app.state.intake.dest_handlers
    handlers["control.command"] = control_command
    handlers["ledger.answer"] = ledger_answer


def _err(status: int, code: str, message: str) -> tuple[int, dict]:
    return status, {"error": {"code": code, "message": message}}


def control_command(app, dest: dict, body: dict, ident: Ident) -> tuple[int, Any]:
    payload = body.get("payload") or {}
    command = {k: payload[k] for k in CONTROL_FIELDS if k in payload}
    command["issued_by"] = "user" if ident.from_ == "hq" else "commander"
    if not command.get("expires_at"):
        # 司令塔の受け口の命令は期限が必須（S-5.3）。指示の期限か登録簿の既定（default_expiry_min）で補う
        base = body.get("expires_at") or ts_expires_at(dest)
        if base is None:
            return _err(422, "expiry_required", "司令塔の受け口からの命令には期限が要る")
        command["expires_at"] = base
    errs = schemas.errors("control-command-request", command)
    if errs:
        return _err(422, "payload_schema", "; ".join(errs[:3]))
    client, path = api_from_handler(dest.get("handler", ""), transport=app.state.intake.transport)
    client = ApiClient(client.base_url, token=read_secret_optional("core-token-intake"),
                       transport=app.state.intake.transport)
    result = client.post(path, command)
    return 200, result


def ledger_answer(app, dest: dict, body: dict, ident: Ident) -> tuple[int, Any]:
    payload = body.get("payload") or {}
    todo_id = payload.get("todo_id")
    answer = payload.get("answer")
    if not isinstance(todo_id, str) or not todo_id:
        return _err(422, "payload_schema", "todo_id が無い")
    if not isinstance(answer, str) or not answer:
        return _err(422, "payload_schema", "answer が無い")
    out: dict[str, Any] = {"answer": answer, "answer_source": "hq" if ident.from_ == "hq" else "relay"}
    if out["answer_source"] == "relay":
        # 司令塔が中継するときは利用者の発言の原文 quote が必須（自己申告で、検証しない。S-6.9）
        q = payload.get("quote")
        if not isinstance(q, str) or not q:
            return _err(422, "payload_schema", "quote が無い（中継の回答には利用者の発言の原文が要る）")
        out["quote"] = q
    elif isinstance(payload.get("quote"), str):
        out["quote"] = payload["quote"]
    if isinstance(payload.get("version"), int):
        out["version"] = payload["version"]
    errs = schemas.errors("todo-answer", out)
    if errs:
        return _err(422, "payload_schema", "; ".join(errs[:3]))
    handler = dest.get("handler", "")
    path = handler.replace("{todo_id}", urlquote(todo_id, safe=""))
    client, api_path = api_from_handler(path, transport=app.state.intake.transport)
    result = client.post(api_path, out)
    return 200, result
