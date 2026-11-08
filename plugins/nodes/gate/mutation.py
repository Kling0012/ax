"""gate.mutation: 対象に合うときだけ変異テストを走らせる（S-7.4）。applies が偽か cmd が空なら skip。終了コード 0 で ok、それ以外は bug。"""
from __future__ import annotations

from runtime.subloop.nodeapi import bug, ok, skip
from runtime.subloop.nodelib import run_verify_job

schema = {
    "type": "object",
    "properties": {"cmd": {"type": ["string", "null"]}, "workdir_id": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$"}, "applies": {"type": ["boolean", "string", "null"]}},
    "required": ["workdir_id"], "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "skip", "error"]}


async def run(ctx, inp):
    w = inp["with"]
    applies = w.get("applies")
    if applies in (False, "false", "0", "", None) or not (w.get("cmd") or "").strip():
        return skip({"reason": "not_applicable"})
    v, err = await run_verify_job(ctx, w["cmd"], w["workdir_id"], label="gate.mutation")
    if err is not None:
        return err
    if v["state"] == "ok" and v["verify_rc"] in (0, None):
        return ok({"rc": 0, "job_id": v["job_id"]})
    return bug("mutation_survived", f"verify.rc={v['verify_rc']}", data={"rc": v["verify_rc"], "job_id": v["job_id"], "log_tail": v["log_tail"][-4000:]})
