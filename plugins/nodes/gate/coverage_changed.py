"""gate.coverage_changed: 変更行のカバレッジが S-17 `gate_changed_coverage` 以上であることを確かめる（S-7.4）。LLM は使わない。

cmd は変更行のカバレッジを `NN%` の形で出すコマンド。changed / diff coverage の値を優先し、目印がなければ % が一つの出力だけを受ける。
cmd が空なら skip。しきい値は with.threshold（0〜1）、無ければ knob gate_changed_coverage、無ければ S-17 の既定。
"""
from __future__ import annotations

import re

from runtime.subloop.nodeapi import bug, error, ok, skip
from runtime.subloop.nodelib import run_verify_job

schema = {
    "type": "object",
    "properties": {"cmd": {"type": ["string", "null"]}, "workdir_id": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$"},
                   "threshold": {"type": ["number", "null"], "minimum": 0, "maximum": 1}},
    "required": ["workdir_id"], "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "skip", "error"]}


async def run(ctx, inp):
    w = inp["with"]
    if not (w.get("cmd") or "").strip():
        return skip({"reason": "no_command"})
    thr = w.get("threshold")
    if thr is None:
        thr = ctx.knobs.get("gate_changed_coverage", ctx.defaults.get("gate_changed_coverage"))
    v, err = await run_verify_job(ctx, w["cmd"], w["workdir_id"], label="gate.coverage_changed")
    if err is not None:
        return err
    if v["verify_rc"] != 0:
        return bug("coverage_command_failed", f"verify.rc={v['verify_rc']}", data={"log_tail": v["log_tail"][-2000:]})
    pcts = re.findall(r"\b(?:changed(?:[-_ ]+(?:lines?|coverage))?|diff(?:[-_ ]+coverage)?)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%", v["log_tail"], re.I)
    if not pcts:
        pcts = re.findall(r"(\d+(?:\.\d+)?)\s*%", v["log_tail"])
    if len(pcts) != 1 or not 0 <= float(pcts[0]) <= 100:
        return error("coverage_unreadable", "変更行の % を一意に読めない")
    got = float(pcts[0]) / 100.0
    if got + 1e-9 < float(thr):
        return bug("coverage_low", f"変更行のカバレッジ {got:.2f} が {float(thr):.2f} 未満", data={"coverage": got, "threshold": float(thr)})
    return ok({"coverage": got, "threshold": float(thr)})
