"""wait.event: 時系列記録の事象を期限付きで待つ（S-7.4）。kind（と subject）が合う事象が来れば ok（data.event）。wait_s までに来なければ
data.outcome=timeout の ok。LLM は使わない。事象の読みは GET /v1/events（作業側の口 8081。after で前回の続きから）。"""
from __future__ import annotations

from axcommon import timeutil
from axcommon.client import ApiError, Unavailable

from runtime.subloop.nodeapi import ok

schema = {
    "type": "object",
    "properties": {"kind": {"type": "string", "minLength": 1}, "subject": {"type": "string"}, "after": {"type": ["string", "null"]},
                   "wait_s": {"type": "integer", "minimum": 0}, "poll_s": {"type": "number", "minimum": 0}},
    "required": ["kind"], "additionalProperties": False,
}
META = {"statuses": ["ok", "error"], "outcomes": ["timeout"]}


def _events(resp) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        return resp.get("events") or []
    return []


async def run(ctx, inp):
    w = inp["with"]
    end = timeutil.now().timestamp() + float(w.get("wait_s", 0))
    poll = float(w.get("poll_s", ctx.svc.poll_s))
    while True:
        try:
            resp = await ctx.call(ctx.svc.timeline.get_events, after=w.get("after"), kind=w["kind"], subject=w.get("subject"), limit=1)
            evs = _events(resp)
            if evs:
                return ok({"event": evs[0], "outcome": "event"})
        except (ApiError, Unavailable):
            pass
        if timeutil.now().timestamp() >= end:
            return ok({"outcome": "timeout"})
        await ctx.sleep(poll)
