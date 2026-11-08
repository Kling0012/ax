"""git.push: ブランチを remote へ push する。外へ影響する型（outputs: git.push）。manifest の outputs に git.push があるときだけ使える（S-7.4）。

- work-tdd は remote `work.git`（ステージング）へ push するだけで、`ax.git` と main/master には書かない（F-18.3）。実行時にも拒否する。
- 未コミットの変更があり commit_message があれば、先にコミットする（worker が commit しない場合の受け皿）。
- ステージングのブランチは項目の持ち物なので、強制 push（`+`）で更新する（再実行しても同じ結果）。
- 二重実行の防止: 鍵は `<remote>:<branch>:<sha>`。reconcile は `git ls-remote` の先頭の SHA が一致するかを見る。
"""
from __future__ import annotations

from runtime.subloop.config import remote_url
from runtime.subloop.nodeapi import bug, error, ok

schema = {
    "type": "object",
    "properties": {"remote": {"type": "string", "minLength": 1}, "branch": {"type": "string", "minLength": 1},
                   "workdir_id": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$"}, "commit_message": {"type": "string"}},
    "required": ["remote", "branch", "workdir_id"], "additionalProperties": False,
}
META = {"statuses": ["ok", "error"], "output": "git.push", "launching": True, "writes_repo": True}
FORBIDDEN_BRANCHES = ("main", "master")
FORBIDDEN_REMOTES = ("ax.git", "origin")
IDENT = ["-c", "user.name=ax", "-c", "user.email=ax@example.com"]


async def run(ctx, inp):
    w = inp["with"]
    if w["remote"] in FORBIDDEN_REMOTES or w["branch"] in FORBIDDEN_BRANCHES or w["branch"].startswith("refs/"):
        return error("push_forbidden", f"remote {w['remote']} / ブランチ {w['branch']} へは書かない（F-18.3）")
    url = remote_url(ctx.svc, w["remote"])
    if not url:
        return error("remote_unregistered", f"remote {w['remote']} が登録されていない（AX_REMOTE_*）")
    cwd = str(ctx.workdir(w["workdir_id"]))
    git = ctx.svc.git
    if w.get("commit_message"):
        st = await git.run(["status", "--porcelain"], cwd=cwd)
        if st.rc == 0 and st.out.strip():
            await git.run(["add", "-A"], cwd=cwd)
            c = await git.run([*IDENT, "commit", "-m", w["commit_message"]], cwd=cwd)
            if c.rc != 0:
                return error("git_commit_failed", c.err[-300:])
    sha = (await git.run(["rev-parse", "HEAD"], cwd=cwd)).out.strip()
    if not sha:
        return error("worktree_broken", "HEAD を読めない")
    key = f"git-push:{w['remote']}:{w['branch']}:{sha}"
    await ctx.launching(key, {"remote": w["remote"], "branch": w["branch"], "sha": sha})
    r = await git.run(["push", url, f"+HEAD:refs/heads/{w['branch']}"], cwd=cwd)
    if r.rc != 0:
        return error("push_failed", r.err[-300:], expected=True)
    return ok({"sha": sha, "branch": w["branch"]}, artifacts={"branch": w["branch"], "sha": sha, "remote": w["remote"]})


async def reconcile(ctx, key):
    ref = ctx.store.get_node_run(ctx.nr_id)["external_ref"]
    url = remote_url(ctx.svc, ref["remote"])
    if not url:
        return None
    r = await ctx.svc.git.run(["ls-remote", url, f"refs/heads/{ref['branch']}"])
    if r.rc != 0:
        return None  # 照合できない → lost
    head = r.out.split()[0] if r.out.strip() else ""
    if head == ref["sha"]:
        return {"found": True, "out": ok({"sha": ref["sha"], "branch": ref["branch"], "reconciled": True},
                                         artifacts={"branch": ref["branch"], "sha": ref["sha"], "remote": ref["remote"]})}
    return {"found": False}
