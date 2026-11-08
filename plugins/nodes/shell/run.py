"""shell.run: コンテナの中でコマンドを 1 つ走らせる（CI など。LLM は使わない）。終了コード 0 で ok、それ以外は bug。
環境変数は最小（秘密を渡さない）。cwd は workdir_id の worktree（省略時は work の根）。"""
from __future__ import annotations

from runtime.subloop.nodeapi import ok, bug

schema = {
    "type": "object",
    "properties": {"cmd": {"type": "string", "minLength": 1}, "workdir_id": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$"},
                   "timeout_s": {"type": "integer", "minimum": 1}},
    "required": ["cmd"], "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "error"]}


async def run(ctx, inp):
    w = inp["with"]
    cwd = str(ctx.workdir(w["workdir_id"])) if w.get("workdir_id") else str(ctx.svc.work_root)
    r = await ctx.svc.shell.run(w["cmd"], cwd=cwd, timeout_s=float(w.get("timeout_s", 300)))
    tail = (r.out + r.err)[-4000:]
    if r.rc == 0:
        return ok({"rc": 0, "log_tail": tail})
    return bug("nonzero_exit", f"終了コード {r.rc}", data={"rc": r.rc, "log_tail": tail})
