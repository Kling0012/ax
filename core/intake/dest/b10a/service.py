"""`emergency` サービス（ax-core の部品。S-12.3、D-11.3）。8080 に /healthz と POST /v1/emergency。

呼べるのは入口（intake）だけ。呼び出し元の資格は core-tokens の表（F-18.4。環境変数 AX_CORE_TOKENS）で `intake` と決まった物。
表が読めない・空のときは 503 で拒否する（閉じる側）。入口の側が `emergency` の指示を `from=hq` だけに絞り、承認（S-6.10）を確かめる。
制御 API には専用トークン（secrets の control-token-emergency）で `issued_by=emergency` の資格で出る（命令の解除に使う）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon import creds
from axcommon.client import ApiClient, AuthenticatedPoolClient, LedgerClient, TimelineClient, service_url
from axcommon.events import EventSink
from axcommon.http import create_app, json_error

from .runner import EmergencyConfig, EmergencyError, EmergencyRunner

VERSION = "0.1.0"
COMPONENT = "emergency"
log = logging.getLogger("emergency")
CALLER = "intake"


def build_runner(environ: dict | None = None, *, transport=None, sink: EventSink | None = None, redact=None) -> EmergencyRunner:
    env = os.environ if environ is None else environ
    cfg = EmergencyConfig(host=env.get("AX_HOST", "unknown"), data_dir=env.get("AX_EMERGENCY_DATA", "/data"),
                          state_dir=env.get("AX_STATE_DIR", "/state"))
    timeline = TimelineClient(service_url("timeline", environ=env), transport=transport)
    if sink is None:
        sink = EventSink(timeline, COMPONENT, cfg.host, Path(cfg.data_dir) / "outbox")
    control = ApiClient(service_url("control", environ=env), token=creds.read_secret_optional("control-token-emergency"), transport=transport)
    ledger = LedgerClient(service_url("ledger", environ=env), transport=transport)
    pool = AuthenticatedPoolClient(service_url("worker-pool", environ=env), token=creds.read_secret_optional("core-token-observer"), transport=transport)
    return EmergencyRunner(cfg, timeline=timeline, control=control, ledger=ledger, pool=pool, sink=sink, redact=redact)


def create_emergency_app(runner: EmergencyRunner, *, core_tokens=None, run_heartbeat_loop: bool = True):
    table_holder = {"t": core_tokens}

    def _table():
        if table_holder["t"] is None:
            p = Path(os.environ.get("AX_CORE_TOKENS", str(Path(os.environ.get("AX_STATE_DIR", "/state")) / "core-tokens.sha256")))
            table_holder["t"] = creds.TokenTable.from_file(p) if p.is_file() else creds.TokenTable(entries={})
        else:
            try:
                table_holder["t"].reload_if_changed()
            except Exception:  # noqa: BLE001
                pass
        return table_holder["t"]

    async def post_emergency(request: Request):
        table = _table()
        if not table.names():
            return json_error(503, "auth_unavailable", "core-tokens の表が読めない")
        if table.identify(creds.parse_bearer(request.headers.get("Authorization"))) != CALLER:
            return json_error(401, "unauthorized", "入口の資格が要る")
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return json_error(400, "bad_json", "本文が JSON でない")
        from axcommon import schemas

        if not isinstance(data, dict) or set(data) - {"scope", "request_id"}:
            return json_error(422, "bad_request", "scope と request_id だけを渡す")
        scope = data.get("scope")
        request_id = data.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 200):
            return json_error(422, "bad_request", "request_id は 1〜200 文字")
        if schemas.errors("ext/b10a/emergency.payload.schema.json", {"scope": scope}):
            return json_error(422, "bad_request", "scope は all、commanders、プラグインの ID")
        try:
            result = await asyncio.to_thread(runner.run, scope, request_id)
        except EmergencyError as e:
            return json_error(e.status, e.code, e.message)
        return JSONResponse(result)

    def extra_health() -> dict:
        t = _table()
        return {"auth": "ok" if t.names() else "unavailable", "sink_queued": runner.sink.queued_total if runner.sink else 0}

    app = create_app(COMPONENT, VERSION, routes=[Route("/v1/emergency", post_emergency, methods=["POST"])], sink=runner.sink,
                     extra_health=extra_health, run_heartbeat_loop=run_heartbeat_loop)
    app.state.runner = runner
    return app
