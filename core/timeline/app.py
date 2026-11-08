"""timeline のアプリの組み立て。設定は環境変数から読む（compose の environment）。

  AX_HOST                   事象の source.host に入れるホスト名（profile の host）
  AX_TIMELINE_DB            既定 /data/timeline.sqlite
  AX_TIMELINE_ARCHIVE       既定 /data/archive（ボリュームの archive/。F-20）
  AX_REDACT_INDEX           既定 /redact/index（索引だけを ro で渡す。S-15.3）
  AX_PII_LIST               既定 /run/secrets/pii-list（無ければ空として動く）
  AX_PLUGIN_TOKENS_FILE     プラグインのトークンのハッシュの一覧（1 行 `<プラグイン ID> <sha256>`）。置き場が文書で決まっていないため
                            既定を持たない（OPEN.md）。未設定の間、8081 は全て 401。
  AX_TIMELINE_EXPORT_INTERVAL_S  書き出しの周期（既定 3600）。0 で自動の書き出しをしない
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from axcommon import creds, timeutil
from axcommon.http import create_app
from axcommon.redact import Redactor

from . import COMPONENT, VERSION, api
from .archive import TimelineArchive
from .store import Store

log = logging.getLogger("timeline")


@dataclass
class Config:
    host: str = "unknown"
    db: str = "/data/timeline.sqlite"
    archive_dir: str = "/data/archive"
    redact_index: str | None = "/redact/index"
    pii_list: str | None = "/run/secrets/pii-list"
    plugin_tokens_file: str | None = None
    export_interval_s: float = 3600
    hot_days: int | None = None
    longpoll_s: float | None = None
    ext_dir: str | None = None

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        e = os.environ if env is None else env
        return cls(host=e.get("AX_HOST", "unknown"), db=e.get("AX_TIMELINE_DB", cls.db), archive_dir=e.get("AX_TIMELINE_ARCHIVE", cls.archive_dir),
                   redact_index=e.get("AX_REDACT_INDEX", cls.redact_index) or None, pii_list=e.get("AX_PII_LIST", cls.pii_list) or None,
                   plugin_tokens_file=e.get("AX_PLUGIN_TOKENS_FILE") or None,
                   export_interval_s=float(e.get("AX_TIMELINE_EXPORT_INTERVAL_S", cls.export_interval_s)))


def _defaults(cfg: Config) -> tuple[int, float]:
    hot, lp = cfg.hot_days, cfg.longpoll_s
    if hot is None or lp is None:
        try:
            from axcommon.defaults import Defaults
            d = Defaults.load()
            hot = d.get("timeline_hot_days") if hot is None else hot
            lp = d.get("longpoll_s") if lp is None else lp
        except Exception:  # noqa: BLE001  設定が読めなくても動く（S-17 の既定値で）
            hot = 30 if hot is None else hot
            lp = 30 if lp is None else lp
    return int(hot), float(lp)


def build(cfg: Config | None = None, *, store: Store | None = None, redactor: Redactor | None = None,
          tokens: creds.TokenTable | None = None, manifest_kinds=None, run_heartbeat_loop: bool = True):
    """(中核の口のアプリ, 作業側の口のアプリ, Service) を返す。"""
    cfg = cfg or Config.from_env()
    hot, lp = _defaults(cfg)
    store = store or Store(cfg.db)
    if redactor is None:
        redactor = Redactor(index_path=cfg.redact_index, pii_list_path=cfg.pii_list)
    if tokens is None and cfg.plugin_tokens_file:
        tokens = creds.TokenTable.from_file(cfg.plugin_tokens_file)
    kw = {"manifest_kinds": manifest_kinds} if manifest_kinds else {}
    svc = api.Service(store, host=cfg.host, redactor=redactor, archive=TimelineArchive(cfg.archive_dir), hot_days=hot, longpoll_s=lp,
                      tokens=tokens, **kw)
    task: dict = {}

    async def export_loop() -> None:
        while True:
            await asyncio.sleep(cfg.export_interval_s)
            try:
                before = timeutil.add_seconds(timeutil.now_ts(), -86400 * svc.hot_days)
                await asyncio.to_thread(lambda: svc.archive.export(svc.store, before_ts=before))
            except Exception:  # noqa: BLE001  書き出しの失敗で記録を止めない
                log.exception("自動の書き出しに失敗")

    async def startup() -> None:
        svc.bind_loop()
        if cfg.export_interval_s > 0:
            task["export"] = asyncio.create_task(export_loop())

    async def shutdown() -> None:
        t = task.get("export")
        if t:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    def health() -> dict:
        rd = svc.redactor
        return {"projection_skipped": store.projection_skipped(), "last_seq": store.max_seq(),
                "redact_index": bool(getattr(rd, "index_ok", True)), "redacted_rejected_total": svc.rejected_total}

    core = create_app(COMPONENT, VERSION, routes=api.build_routes(svc, work_side=False), sink=svc.sink, extra_health=health,
                      ext_dir=cfg.ext_dir or str(Path(__file__).parent / "ext"), on_startup=[startup], on_shutdown=[shutdown],
                      run_heartbeat_loop=run_heartbeat_loop)

    def work_health() -> dict:
        return {"plugin_tokens": svc.tokens is not None}

    async def work_startup() -> None:
        # 8081 のアプリは core と同じイベントループで動く（同じプロセスの 2 つの uvicorn）。Condition は core の物を共有する
        return None

    work = create_app(COMPONENT, VERSION, routes=api.build_routes(svc, work_side=True), sink=None, extra_health=work_health,
                      run_heartbeat_loop=False, on_startup=[work_startup])
    core.state.service = svc
    work.state.service = svc
    return core, work, svc
