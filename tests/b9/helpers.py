"""tests/b9 の補助。"""
from __future__ import annotations

from datetime import timedelta

from axcommon import timeutil
from tests.fakes.model_gateway import Reply


def in_min(minutes: float) -> str:
    return timeutil.format_ts(timeutil.now() + timedelta(minutes=minutes))


def reply(*actions) -> Reply:
    from tests.fakes.b9.fake_pool import decision

    return Reply(decision(*actions))


def noop(reason: str = "様子を見る") -> dict:
    return {"type": "noop", "reason": reason}


def seq_of(world, kind: str, **match) -> list[int]:
    out = []
    for e in world.timeline.store.list_events(after=0, kinds=[kind], limit=100000):
        if all((e["data"].get(k) == v) for k, v in match.items()):
            out.append(e["seq"])
    return out


def todos(world, **q) -> list[dict]:
    return world.ledger.list_todos(**q)
