"""通知の周期処理（質問の走査、保持分の再配送、朝の要約）と、HQ に組み込む部品の組み立て。

1 つの処理の失敗で他を止めない（fail-soft）。台帳や ntfy に届かなくても、HQ の画面と他の機能は動く。
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from axcommon.client import ApiError, LedgerClient, Transport

from . import DEST_NTFY
from .actions import NotifyActions
from .digest import LedgerDigestSources, MorningDigest
from .dispatcher import Dispatcher
from .guard import Guard
from .intake import IntakeClient
from .ntfy import NtfyClient
from .question import LedgerPort, QuestionNotifier, default_renotify_h
from .store import NotifyStore

log = logging.getLogger("hq.notify.loop")


@dataclass
class NotifyHub:
    store: NotifyStore
    dispatcher: Dispatcher
    questions: QuestionNotifier
    digest: MorningDigest
    actions: NotifyActions
    interval_s: float = 60.0
    last: dict | None = None

    def tick(self) -> dict:
        out: dict[str, Any] = {}
        for name, fn in (("questions", self.questions.tick), ("deliver", self.dispatcher.deliver_due), ("digest", self.digest.run_if_due)):
            try:
                out[name] = fn()
            except Exception as e:  # noqa: BLE001  1 つの失敗で他を止めない
                out[name] = {"error": type(e).__name__}
                log.error("通知の周期処理 %s の失敗: %s", name, type(e).__name__)
        self.last = out
        return out

    def apply_knobs(self, knobs: dict) -> list[str]:
        """制御 API の knob の値を取り込む（S-22.3、S-22.5）。範囲外の値は無視する。取り込めた名前を返す。
        どのプラグインの knob かは未定（OPEN.md の b10a）。呼ぶ側（HQ の拡張）が値を渡す。"""
        applied = []
        if "question_renotify_h" in knobs and self.questions.set_renotify_h(knobs["question_renotify_h"]):
            applied.append("question_renotify_h")
        if "morning_digest_time_jst" in knobs and isinstance(knobs["morning_digest_time_jst"], str) \
                and self.digest.set_time(knobs["morning_digest_time_jst"]):
            applied.append("morning_digest_time_jst")
        return applied

    def stats(self) -> dict:
        return {"notifications": self.store.counts(), "renotify_h": self.questions.renotify_h, "digest_time": self.digest.time_text,
                "guard_verifiable": self.dispatcher.guard.verifiable, "last": self.last}


class Runner:
    """hub.tick() を一定の間隔で回す小さなスレッド。"""

    def __init__(self, hub: NotifyHub):
        self.hub = hub
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hq-notify", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.hub.tick()
            self._stop.wait(self.hub.interval_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def build_hub(*, environ: dict | None = None, sink=None, transport: Transport | None = None, intake_transport=None,
              redactor=None, now: Callable[[], Any] | None = None, defaults=None) -> NotifyHub:
    """環境変数から組み立てる。値（トークンなど）は環境変数に置かず、ファイルの場所だけを使う。

    AX_NTFY_ENDPOINT / AX_NTFY_TOPIC  profile の ntfy_endpoint / ntfy_topic（profile.env 経由。F-18.6）
    AX_LEDGER_URL                      台帳の読み取り（既定 http://ledger:8080）
    AX_HQ_DATA                         HQ のソケットの bind の下（既定 /run/hq/notify）。notify.sqlite を置く
    AX_HQ_BASE_URL                     通知の「開く」の基点（未設定なら click を付けない）
    AX_HQ_NOTIFY_INTERVAL_S            周期（既定 60）
    """
    env = os.environ if environ is None else environ
    if defaults is None:
        try:
            from axcommon.defaults import Defaults

            defaults = Defaults.load()
        except Exception:  # noqa: BLE001
            defaults = None
    store = NotifyStore(Path(env.get("AX_HQ_DATA", "/run/hq/notify")) / "notify.sqlite")
    ntfy = NtfyClient(env.get("AX_NTFY_ENDPOINT", "http://ntfy.invalid:8908"), env.get("AX_NTFY_TOPIC", "hub"), transport=transport)
    guard = Guard(redactor)
    disp = Dispatcher(store, {DEST_NTFY: ntfy}, guard=guard, sink=sink, now=now, hq_base_url=env.get("AX_HQ_BASE_URL"), defaults=defaults)
    ledger_client = LedgerClient(env.get("AX_LEDGER_URL", "http://ledger:8080"), transport=transport)
    port = LedgerPort(ledger_client)
    time_text = "07:00 JST"
    if defaults is not None:
        try:
            time_text = str(defaults.get("morning_digest_time_jst"))
        except Exception:  # noqa: BLE001
            pass
    hub = NotifyHub(
        store=store, dispatcher=disp,
        questions=QuestionNotifier(port, store, disp, renotify_h=default_renotify_h(defaults), now=now),
        digest=MorningDigest(LedgerDigestSources(ledger_client), store, disp, time_jst=time_text, now=now, sink=sink),
        actions=NotifyActions(port, IntakeClient(intake_transport), store, now=now),
        interval_s=float(env.get("AX_HQ_NOTIFY_INTERVAL_S", "60") or 60))
    return hub
