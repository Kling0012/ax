"""tests/b8 の補助。オブザーバーを偽の時系列記録・偽の台帳・偽の時計で組む。"""
from __future__ import annotations

import os
from pathlib import Path

from axcommon import ids
from axcommon.client import LedgerClient, TimelineClient
from axcommon.events import EventSink
from core.observer.observer import Observer
from core.observer.settings import Settings
from tests.fakes.b8.fake_services import LEDGER, TIMELINE, FakeLedger, FakeTimeline


def fmt(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class Rig:
    """オブザーバーと偽の部品一式。clock は fake_clock、transport は fake_transport。"""

    def __init__(self, tmp: Path, clock, transport, *, redact=None, **settings):
        self.tmp = tmp
        self.state = tmp / "state"
        self.data = tmp / "data"
        for d in (self.state / "hostwatch" / "registered", self.state / "host-requests", self.state / "cutover", self.data):
            d.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.tl = FakeTimeline(transport, clock)
        self.ledger = FakeLedger(transport)
        self.s = Settings(host="h", state_dir=str(self.state), data_dir=str(self.data), timeline_url=TIMELINE, ledger_url=LEDGER,
                          heartbeat_s=60, stale_beats=3, alert_dedup_min=30, **settings)
        timeline = TimelineClient(TIMELINE, transport=transport)
        ledger = LedgerClient(LEDGER, transport=transport)
        self.sink = EventSink(timeline, "observer", "h", self.data / "outbox")
        self.obs = Observer(self.s, timeline, self.sink, ledger=ledger, redact=redact)
        self.probe_ok: dict[str, bool] = {}
        self.obs.sources._probe = lambda comp: self.probe_ok.get(comp, True)

    def register(self, *names: str):
        for n in names:
            f = self.state / "hostwatch" / "registered" / n
            f.touch()
            t = self.clock.now().timestamp()
            os.utime(f, (t, t))  # 偽の時計に合わせる（登録の時刻を使う検査のため）

    def now_ts(self, minus_s: float = 0) -> str:
        from datetime import timedelta

        return fmt(self.clock.now() - timedelta(seconds=minus_s))

    def beat(self, comp: str, age_s: float = 0, lag_s: float = 0):
        self.tl.beat(comp, self.now_ts(age_s), lag_s)

    def alerts(self, check: str | None = None) -> list[dict]:
        evs = self.tl.of_kind("health.alert")
        return [e for e in evs if check is None or e["data"]["check"] == check]

    def requests(self) -> list[str]:
        return sorted(p.name for p in (self.state / "host-requests").glob("*.req"))
