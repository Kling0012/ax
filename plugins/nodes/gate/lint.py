"""gate.lint: 静的検査（lint）。cmd を verify のジョブで走らせ、終了コード 0 で ok、それ以外で bug（S-7.4）。LLM は使わない。cmd が空なら skip。"""
from __future__ import annotations

from runtime.subloop.nodeapi import bug, ok, skip
from runtime.subloop.nodelib import run_verify_job

schema = {
    "type": "object",
    "properties": {"cmd": {"type": ["string", "null"]}, "workdir_id": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$"}},
    "required": ["workdir_id"], "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "skip", "error"]}


async def run(ctx, inp):
    w = inp["with"]
    if not (w.get("cmd") or "").strip():
        return skip({"reason": "no_command"})
    v, err = await run_verify_job(ctx, w["cmd"], w["workdir_id"], label="gate.lint")
    if err is not None:
        return err
    if v["state"] == "ok" and v["verify_rc"] in (0, None):
        return ok({"rc": 0, "job_id": v["job_id"]})
    return bug("lint_failed", f"verify.rc={v['verify_rc']}", data={"rc": v["verify_rc"], "job_id": v["job_id"], "log_tail": v["log_tail"][-4000:]})
