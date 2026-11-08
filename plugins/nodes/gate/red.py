"""gate.red: 実装前の worktree でテストを走らせ、意図どおり失敗することを確かめる（S-7.4、D-6.6）。LLM は使わない。

- 終了コード 0（全部通る）は bug（最初から通るテストは test_design へ差し戻す）。
- 出力に通ったテストがある（passed > 0）なら bug。ImportError・構文エラーなどの収集の失敗で赤になっているだけなら bug（意図した理由で赤になっていない）。
- 通ったら、tests_dir の下のファイルの内容ハッシュを data.snapshot に残す（gate.tests_unchanged が比べる）。
- cmd が空なら skip（検証コマンドが決まっていない項目。OPEN.md）。skip でも snapshot は残す。
"""
from __future__ import annotations

from runtime.subloop.nodeapi import bug, ok, skip
from runtime.subloop.nodelib import INVALID_RED, parse_test_counts, run_verify_job, snapshot_tree

schema = {
    "type": "object",
    "properties": {"cmd": {"type": ["string", "null"]}, "workdir_id": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$"},
                   "tests_dir": {"type": "string", "minLength": 1}},
    "required": ["workdir_id", "tests_dir"], "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "skip", "error"]}


async def run(ctx, inp):
    w = inp["with"]
    snap = snapshot_tree(ctx.workdir(w["workdir_id"]), w["tests_dir"])
    if not snap:
        return bug("no_tests", f"{w['tests_dir']} の下にテストが無い。test_design に差し戻す")
    if not (w.get("cmd") or "").strip():
        return skip({"reason": "no_command", "snapshot": snap})
    v, err = await run_verify_job(ctx, w["cmd"], w["workdir_id"], label="gate.red")
    if err is not None:
        return err
    log = v["log_tail"]
    rc = v["verify_rc"]
    counts = parse_test_counts(log)
    if rc == 0:
        return bug("red_all_pass", "実装の前にテストが全部通る", data={"rc": rc, "counts": counts})
    if counts.get("passed"):
        return bug("red_already_passing", f"最初から通るテストがある（{counts['passed']} 件）", data={"rc": rc, "counts": counts})
    if INVALID_RED.search(log) and not counts.get("failed"):
        return bug("red_invalid_reason", "ImportError・構文エラーなど、意図した理由でない失敗", data={"rc": rc, "counts": counts})
    return ok({"rc": rc, "counts": counts, "snapshot": snap})
