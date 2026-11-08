"""投影の作り方（S-3.6、D-3）。

PROJECTORS: kind -> Projector(name, key, update)。
  key(event)           行の key。必須の項目が無ければ Skip。
  update(current, ev)  (行の現在の値 or None, 事象) -> 新しい値。純粋な関数。現在時刻・乱数・外部を読まない。
Skip が出た事象は、その投影の更新だけ飛ばす（追記は成功。projection_skipped が 1 増える。fail-soft）。
値の入れ物は dict。行の upto_seq は、更新した事象の seq（store が付ける）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

PROJECTION_NAMES = ("plugins", "runs", "jobs", "controls", "health")
RUN_ACTIVE_STATES = frozenset({"launching", "submitted", "running"})
JOB_ACTIVE_STATES = frozenset({"launching", "submitted", "running", "stalled"})
CONTROL_ACTIVE_STATES = frozenset({"active", "expired_pending"})
# 命令の項目（S-5.3）。controls の行の value に写す
COMMAND_FIELDS = ("target", "op", "args", "reason", "issued_by", "issued_at", "expires_at", "on_expiry")
ACTIVE_NAMES = frozenset({"runs", "jobs", "controls"})  # active=true の絞り込みができる名前


class Skip(Exception):
    """必須の項目が無く、この投影の更新を飛ばす。"""


@dataclass(frozen=True)
class Projector:
    name: str
    key: Callable[[dict], str]
    update: Callable[[dict | None, dict], dict]


def _data(ev: dict) -> dict:
    d = ev.get("data")
    if not isinstance(d, dict):
        raise Skip("data が object でない")
    return d


def _need(d: dict, name: str) -> Any:
    v = d.get(name)
    if v is None or v == "":
        raise Skip(f"data.{name} が無い")
    return v


# --- plugins ---------------------------------------------------------------------------------
def _plugin_key(ev: dict) -> str:
    _need(_data(ev), "mode")
    s = ev.get("subject")
    if not s:
        raise Skip("subject が無い")
    return s


def _plugin_base(cur: dict | None) -> dict:
    return dict(cur) if cur else {"manifest_version": None, "mode": None, "registered_at": None, "mode_changed_at": None}


def _plugin_registered(cur: dict | None, ev: dict) -> dict:
    d = _data(ev)
    v = _plugin_base(cur)
    v["manifest_version"] = d.get("manifest_version")
    v["mode"] = _need(d, "mode")
    v["registered_at"] = ev["ts"]
    return v


def _plugin_mode(cur: dict | None, ev: dict) -> dict:
    d = _data(ev)
    v = _plugin_base(cur)
    v["mode"] = _need(d, "mode")
    v["mode_changed_at"] = ev["ts"]
    return v


# --- runs / jobs -----------------------------------------------------------------------------
def _run_key(ev: dict) -> str:
    _need(_data(ev), "state")
    s = ev.get("subject")
    if not s:
        raise Skip("subject が無い")
    return s


def _run_update(cur: dict | None, ev: dict) -> dict:
    d = _data(ev)
    state = _need(d, "state")
    v = dict(cur) if cur else {"first_ts": ev["ts"]}
    v.update({
        "subloop": ev.get("subloop"),
        "project": ev.get("project"),
        "state": state,
        "reason": d.get("reason"),
        "last_ts": ev["ts"],
        "active": state in RUN_ACTIVE_STATES,
    })
    return {k: v[k] for k in ("subloop", "project", "state", "reason", "first_ts", "last_ts", "active")}


def _job_key(ev: dict) -> str:
    d = _data(ev)
    _need(d, "state")
    return str(_need(d, "job_id"))


def _job_update(cur: dict | None, ev: dict) -> dict:
    d = _data(ev)
    state = _need(d, "state")
    v = dict(cur) if cur else {"model": None, "role": None, "first_ts": ev["ts"]}
    # model と role は、事象に入っているときだけ置き換える（待ちの状態の事象はまだ持たないことがある）
    if d.get("model") is not None:
        v["model"] = d["model"]
    if d.get("role") is not None:
        v["role"] = d["role"]
    v["state"] = state
    v["last_ts"] = ev["ts"]
    v["active"] = state in JOB_ACTIVE_STATES
    return {k: v[k] for k in ("state", "model", "role", "first_ts", "last_ts", "active")}


# --- controls --------------------------------------------------------------------------------
def _command(ev: dict) -> dict:
    cmd = _data(ev).get("command")
    if not isinstance(cmd, dict):
        raise Skip("data.command が無い")
    return cmd


def _control_key(ev: dict) -> str:
    cid = _command(ev).get("id")
    if not cid:
        raise Skip("data.command.id が無い")
    return str(cid)


def _control_base(cur: dict | None, ev: dict) -> dict:
    if cur:
        return dict(cur)
    cmd = _command(ev)
    v = {f: cmd.get(f) for f in COMMAND_FIELDS}
    v.update({"state": None, "applied_at": None, "active": False})
    return v


def _control_finish(v: dict) -> dict:
    v["active"] = v["state"] in CONTROL_ACTIVE_STATES
    order = list(COMMAND_FIELDS) + ["state", "applied_at", "active"]
    return {k: v.get(k) for k in order}


def _control_issued(cur: dict | None, ev: dict) -> dict:
    cmd = _command(ev)
    v = dict(cur) if cur else {"applied_at": None}
    for f in COMMAND_FIELDS:
        v[f] = cmd.get(f)
    v["state"] = "active"
    return _control_finish(v)


def _control_applied(cur: dict | None, ev: dict) -> dict:
    v = _control_base(cur, ev)
    v["applied_at"] = ev["ts"]
    if v.get("state") is None:
        v["state"] = "active"  # issued を見ていない行。state は変えない規則だが、空のままにしない
    return _control_finish(v)


def _control_renewed(cur: dict | None, ev: dict) -> dict:
    v = _control_base(cur, ev)
    v["expires_at"] = _command(ev).get("expires_at")
    v["state"] = "active"
    return _control_finish(v)


def _control_state(state: str) -> Callable[[dict | None, dict], dict]:
    def upd(cur: dict | None, ev: dict) -> dict:
        v = _control_base(cur, ev)
        v["state"] = state
        return _control_finish(v)
    return upd


# --- health ----------------------------------------------------------------------------------
def _health_key(ev: dict) -> str:
    c = (ev.get("source") or {}).get("component")
    if not c:
        raise Skip("source.component が無い")
    return c


def _health_update(cur: dict | None, ev: dict) -> dict:
    d = _data(ev)
    return {"host": (ev.get("source") or {}).get("host"), "last_beat_at": ev["ts"], "uptime_s": d.get("uptime_s"), "lag_s": d.get("lag_s")}


PROJECTORS: dict[str, Projector] = {
    "plugin.registered": Projector("plugins", _plugin_key, _plugin_registered),
    "plugin.mode": Projector("plugins", _plugin_key, _plugin_mode),
    "run.state": Projector("runs", _run_key, _run_update),
    "job.state": Projector("jobs", _job_key, _job_update),
    "control.issued": Projector("controls", _control_key, _control_issued),
    "control.applied": Projector("controls", _control_key, _control_applied),
    "control.renewed": Projector("controls", _control_key, _control_renewed),
    "control.expired_pending": Projector("controls", _control_key, _control_state("expired_pending")),
    "control.released": Projector("controls", _control_key, _control_state("released")),
    # control.rejected は行を作らない（命令は成立していない）ので、表に載せない
    "health.beat": Projector("health", _health_key, _health_update),
}


def apply_event(rows: dict[tuple[str, str], tuple[dict, int]], ev: dict, seq: int) -> bool | None:
    """メモリ上の rows[(name, key)] = (value, upto_seq) に 1 件を適用する。
    表にない kind は None（影響なし）、更新したら True、飛ばしたら False を返す。"""
    p = PROJECTORS.get(ev.get("kind", ""))
    if p is None:
        return None
    try:
        key = p.key(ev)
        cur = rows.get((p.name, key))
        value = p.update(cur[0] if cur else None, ev)
    except Skip:
        return False
    rows[(p.name, key)] = (value, seq)
    return True
