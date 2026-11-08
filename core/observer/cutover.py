"""切り替えの記録の取り込み（S-24.1 の 7、D-10.1）。

`${ax_state_dir}/cutover/cutover-state.jsonl`（コンテナでは /state/cutover/。読み取り専用）の未取り込みの行を、observe_s ごとに
時系列記録へ POST /v1/events で送る。取り込み済みの行数は自分のボリュームの cutover.cursor に持つ（無くなれば先頭から全部送る。
同じ id は時系列記録が無視するので重複しない。S-3.5）。送る前に S-15.3 の秘密検査（data と subject）を通す。
形が壊れた行・許可外の kind の行は取り込まず error.classified（code cutover_record_invalid）を書き、他の行は続ける。
時系列記録に届かないときは、その行から先を次の周期に回す（ファイルの追記は止まらない）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from axcommon import schemas
from axcommon.client import ApiError, TimelineClient, Unavailable
from axcommon.events import EventSink
from axcommon.ids import is_ulid

from .sources import json_load, json_save, parse_ts_or_none

KINDS = ("cutover.state", "removal.state", "removal.check")
PRINCIPAL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
VIAS = ("local", "ssh", "tailnet", "hq")
MAX_LINE_BYTES = 4000  # 4000 バイト未満（改行を含めない行の長さではなく、改行を含む 1 行の長さ。cutover_note.py と同じ）
CODE = "cutover_record_invalid"
FILE_NAME = "cutover-state.jsonl"


def check_line(raw: bytes) -> tuple[dict | None, str | None]:
    """行 -> (事象, None) か (None, 理由)。理由は短い定型文で、行の中身を含めない。"""
    if len(raw) + 1 >= MAX_LINE_BYTES:
        return None, "line_too_long"
    try:
        ev = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "not_json"
    if not isinstance(ev, dict):
        return None, "not_object"
    if ev.get("kind") not in KINDS:
        return None, "kind_not_allowed"
    if not is_ulid(ev.get("id")):
        return None, "bad_id"
    src = ev.get("source")
    if not isinstance(src, dict):
        return None, "no_source"
    if src.get("component") != "cutover-operator":
        return None, "bad_component"
    if not (isinstance(src.get("principal"), str) and PRINCIPAL_RE.fullmatch(src["principal"])):
        return None, "bad_principal"
    sess = src.get("session")
    if not (isinstance(sess, str) and 0 < len(sess) <= 64 and sess.isprintable()):
        return None, "bad_session"
    if src.get("via") not in VIAS:
        return None, "bad_via"
    if not is_ulid(ev.get("cause")):
        return None, "bad_cause"
    if ev.get("project") is not None or ev.get("subloop") is not None:
        return None, "project_subloop_not_null"
    data = ev.get("data")
    if not isinstance(data, dict) or "principal" in data:
        return None, "bad_data"
    target_key = "target" if ev["kind"] == "cutover.state" else "component_id"
    if not isinstance(data.get(target_key), str) or not data[target_key]:
        return None, "bad_target"
    if not isinstance(data.get("result"), str) or (ev["kind"] == "cutover.state" and data["result"] not in ("ok", "failed", "skipped", "waiting")):
        return None, "bad_result"
    if "reason" in data and not isinstance(data["reason"], str):
        return None, "bad_reason"
    if not isinstance(ev.get("subject"), str) or not ev["subject"]:
        return None, "no_subject"
    if ev["kind"] == "cutover.state" and ev["subject"] == "cutover-time":
        at = data.get("at")
        if not isinstance(at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", at) or parse_ts_or_none(at) is None:
            return None, "bad_cutover_time"
    errs = schemas.errors("event", ev)
    if errs:
        return None, "schema"
    return ev, None


class CutoverIngester:
    def __init__(self, state_dir: str | Path, data_dir: str | Path, timeline: TimelineClient, sink: EventSink,
                 redact: Callable[[Any], Any] | None = None):
        self.path = Path(state_dir) / "cutover" / FILE_NAME
        self.cursor = Path(data_dir) / "cutover.cursor"
        self.timeline, self.sink, self._redact = timeline, sink, redact
        self.last = {"posted": 0, "invalid": 0, "deferred": False}

    def _lines(self) -> list[bytes]:
        try:
            blob = self.path.read_bytes()
        except OSError:
            return []
        parts = blob.split(b"\n")
        return parts[:-1]  # 末尾の改行のない書きかけの行は、次の周期に回す

    def run(self) -> dict:
        stats = {"posted": 0, "invalid": 0, "deferred": False}
        self.last = stats
        lines = self._lines()
        done = json_load(self.cursor, {}).get("lines", 0)
        if not isinstance(done, int) or done < 0 or done > len(lines):
            done = 0  # 置き換わった・縮んだ: 先頭から（同じ id は無視される）
        for i in range(done, len(lines)):
            raw = lines[i]
            if raw.strip() == b"":
                done = i + 1
                continue
            ev, why = check_line(raw)
            if ev is not None:
                ev = self._scrub(ev)
                try:
                    self.timeline.post_events(ev)
                    stats["posted"] += 1
                except Unavailable:
                    stats["deferred"] = True
                    break
                except ApiError as e:
                    ev, why = None, f"rejected_{e.status}"
            if ev is None:
                stats["invalid"] += 1
                self.sink.emit("error.classified", f"cutover-line-{i + 1}",
                               {"class": "artifact.bug", "code": CODE, "message": f"line {i + 1}: {why}"})
            done = i + 1
        json_save(self.cursor, {"lines": done})
        return stats

    def _scrub(self, ev: dict) -> dict:
        redact = self._redact
        if redact is None:
            from axcommon.redact import from_environment

            redact = from_environment().as_hook()
        out = dict(ev)
        out["data"] = redact(ev["data"])
        out["subject"] = redact(ev["subject"])
        return out


def latest_cutover_time(events: list[dict]) -> str | None:
    """切り替えの時刻（S-24.1 の 5）。subject が cutover-time の記録のうち ts が最も新しい 1 件の data.at。"""
    best = None
    for ev in events:
        if ev.get("subject") == "cutover-time" and ev.get("kind") == "cutover.state" and isinstance((ev.get("data") or {}).get("at"), str):
            if best is None or str(ev.get("ts")) > str(best.get("ts")):
                best = ev
    return best["data"]["at"] if best else None
