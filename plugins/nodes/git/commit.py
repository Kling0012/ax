"""git.commit: worktree の変更をコミットする（ローカルだけ。外へ影響しない）。変更が無ければ skip。"""
from __future__ import annotations

from runtime.subloop.nodeapi import error, ok, skip

schema = {
    "type": "object",
    "properties": {"workdir_id": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$"}, "message": {"type": "string", "minLength": 1}},
    "required": ["workdir_id", "message"], "additionalProperties": False,
}
META = {"statuses": ["ok", "skip", "error"], "writes_repo": True}
IDENT = ["-c", "user.name=ax", "-c", "user.email=ax@example.com"]


async def run(ctx, inp):
    w = inp["with"]
    cwd = str(ctx.workdir(w["workdir_id"]))
    git = ctx.svc.git
    r = await git.run(["add", "-A"], cwd=cwd)
    if r.rc != 0:
        return error("git_add_failed", r.err[-300:])
    r = await git.run(["diff", "--cached", "--quiet"], cwd=cwd)
    if r.rc == 0:
        return skip({"committed": False})
    r = await git.run([*IDENT, "commit", "-m", w["message"]], cwd=cwd)
    if r.rc != 0:
        return error("git_commit_failed", r.err[-300:])
    sha = await git.run(["rev-parse", "HEAD"], cwd=cwd)
    return ok({"committed": True, "sha": sha.out.strip()})
