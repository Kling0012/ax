"""B1 の試験用の事象の作り方。時刻は呼び出し側が決める（現在時刻に依存しない）。"""
from __future__ import annotations

from axcommon import ids

_n = [0]


def make_event(kind: str, subject: str, data: dict | None = None, *, ts: str = "2026-11-05T12:00:00.000Z", component: str = "sl-demo",
               host: str = "hub-host", project: str | None = "demo", subloop: str | None = "demo-loop", event_id: str | None = None,
               source_extra: dict | None = None, corr: str | None = None, cause: str | None = None) -> dict:
    _n[0] += 1
    source = {"component": component, "host": host}
    if source_extra:
        source.update(source_extra)
    return {
        "id": event_id or ids.new_ulid(now_ms=1_700_000_000_000 + _n[0]),
        "ts": ts,
        "source": source,
        "project": project,
        "subloop": subloop,
        "kind": kind,
        "subject": subject,
        "data": data or {},
        "corr": corr,
        "cause": cause,
    }


def ts_of(day: int, hour: int = 12, minute: int = 0, second: int = 0, month: int = 11) -> str:
    return f"2026-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}.000Z"


def full_sequence() -> list[dict]:
    """T-117 の固定の事象列: plugin.registered、plugin.mode、run.state の全状態、job.state、control.* の全種、health.beat。"""
    out: list[dict] = []
    t = 0

    def nxt() -> str:
        nonlocal t
        t += 1
        return f"2026-11-05T12:{t // 60:02d}:{t % 60:02d}.000Z"

    out.append(make_event("plugin.registered", "issue-automation", {"manifest_version": "1.0.0", "mode": "run"}, ts=nxt(), component="control"))
    out.append(make_event("plugin.mode", "issue-automation", {"mode": "draining"}, ts=nxt(), component="control"))
    for i, st in enumerate(["planned", "launching", "submitted", "running", "completed", "failed", "timed_out", "cancelled", "lost", "skipped",
                            "ejected", "missed"]):
        out.append(make_event("run.state", f"RUN{i:02d}", {"state": st, "reason": f"r-{st}" if st in ("failed", "skipped", "ejected") else None},
                              ts=nxt(), subloop="issue-automation", project="fixture-project"))
    for st in ["queued", "launching", "submitted", "running", "stalled", "completed"]:
        out.append(make_event("job.state", "pool", {"job_id": f"J-{st}", "state": st, "model": "m1", "role": "impl"}, ts=nxt(), component="worker-pool"))
    cmd = {"id": "CMD1", "target": "issue-automation", "op": "pause", "args": {}, "reason": "t", "issued_by": "manager", "issued_at": "2026-11-05T12:00:30.000Z",
           "expires_at": "2026-11-05T13:00:00.000Z", "on_expiry": "resume"}
    out.append(make_event("control.issued", "CMD1", {"command": cmd}, ts=nxt(), component="control"))
    out.append(make_event("control.applied", "CMD1", {"command": {"id": "CMD1"}}, ts=nxt(), component="control"))
    out.append(make_event("control.renewed", "CMD1", {"command": dict(cmd, expires_at="2026-11-05T14:00:00.000Z")}, ts=nxt(), component="control"))
    out.append(make_event("control.expired_pending", "CMD1", {"command": {"id": "CMD1"}}, ts=nxt(), component="control"))
    cmd2 = dict(cmd, id="CMD2")
    out.append(make_event("control.issued", "CMD2", {"command": cmd2}, ts=nxt(), component="control"))
    out.append(make_event("control.released", "CMD2", {"command": {"id": "CMD2"}}, ts=nxt(), component="control"))
    out.append(make_event("control.rejected", "CMD3", {"command": dict(cmd, id="CMD3")}, ts=nxt(), component="control"))
    out.append(make_event("health.beat", "timeline", {"uptime_s": 10, "lag_s": 0}, ts=nxt(), component="timeline"))
    out.append(make_event("health.beat", "ledger", {"uptime_s": 20, "lag_s": 1}, ts=nxt(), component="ledger"))
    return out
