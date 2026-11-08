"""gate.tests_unchanged: 指定したテストの置き場が、gate.red の時点から変わっていないことを確かめる（S-7.4、S-7.6）。LLM は使わない。
変更・削除・追加があれば bug（実装がテストを変えた）。snapshot は gate.red の data.snapshot（`{{nodes.….data.snapshot}}`）。"""
from __future__ import annotations

from runtime.subloop.nodeapi import bug, error, ok
from runtime.subloop.nodelib import snapshot_tree

schema = {
    "type": "object",
    "properties": {"workdir_id": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$"}, "tests_dir": {"type": "string", "minLength": 1},
                   "snapshot": {"type": ["object", "null"], "additionalProperties": {"type": "string"}}},
    "required": ["workdir_id", "tests_dir", "snapshot"], "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "error"]}


async def run(ctx, inp):
    w = inp["with"]
    before = w.get("snapshot")
    if not isinstance(before, dict):
        return error("snapshot_missing", "gate.red の snapshot が無い")
    now = snapshot_tree(ctx.workdir(w["workdir_id"]), w["tests_dir"])
    changed = sorted(p for p in before if p in now and now[p] != before[p])
    deleted = sorted(p for p in before if p not in now)
    added = sorted(p for p in now if p not in before)
    if changed or deleted or added:
        return bug("tests_changed", "テストが変わっている", data={"changed": changed, "deleted": deleted, "added": added})
    return ok({"files": len(now)})
