"""git.worktree: 項目ごとに worktree とブランチを `work/job-<wd>/` に作る（S-7.4、F-18.2）。LLM は使わない。

出力: data.workdir_id（`wt-` で始まる）、data.branch、data.base_sha。以後の worker.run、verify.cmd、gate.* はこの workdir_id を渡して同じ worktree を使う（S-8.6）。
- 元のリポジトリは `work/repos/<名前>.git`（bare）に 1 回だけ clone し、次からは fetch する。リポジトリの URL は Services.repos（AX_REPO_*）から引く。
- 再実行しても同じ workdir_id・同じ worktree を返す（冪等）。項目に resume_ref があれば、その workdir_id を使い、あれば作り直さない（S-8.12 の 8）。
"""
from __future__ import annotations

from pathlib import Path

from runtime.subloop.config import repo_url
from runtime.subloop.nodeapi import bug, error, ok
from runtime.subloop.nodelib import short_hash, slug

schema = {
    "type": "object",
    "properties": {"repo": {"type": "string", "minLength": 1}, "base_ref": {"type": "string", "minLength": 1}, "branch": {"type": "string", "minLength": 1}},
    "required": ["repo", "base_ref", "branch"], "additionalProperties": False,
}
META = {"statuses": ["ok", "bug", "error"], "writes_repo": True}


def _workdir_id(ctx) -> str:
    ref = (ctx.item or {}).get("resume_ref")
    if isinstance(ref, dict) and ref.get("workdir_id"):
        return str(ref["workdir_id"])
    return "wt-" + short_hash(ctx.svc.plugin_id, ctx.item_id)


async def run(ctx, inp):
    w = inp["with"]
    git = ctx.svc.git
    wd = _workdir_id(ctx)
    path = ctx.workdir(wd)
    branch = w["branch"]
    if any(c in branch for c in (" ", "..", "~", "^", ":", "?", "*", "[", "\\")) or branch.startswith(("-", "/")):
        return bug("branch_invalid", "ブランチ名が不正")
    if (path / ".git").exists():
        r = await git.run(["rev-parse", "HEAD"], cwd=str(path))
        if r.rc != 0:
            return error("worktree_broken", r.err[-300:])
        base = await git.run(["merge-base", "HEAD", w["base_ref"]], cwd=str(path))
        return ok({"workdir_id": wd, "branch": branch, "base_sha": (base.out.strip() or r.out.strip()), "reused": True})
    url = repo_url(ctx.svc, w["repo"])
    if not url:
        return error("repo_unregistered", f"リポジトリ {w['repo']} が登録されていない（AX_REPO_*）")
    base_repo = Path(ctx.svc.work_root) / "repos" / (slug(w["repo"]) + ".git")
    if (base_repo / "HEAD").exists():
        r = await git.run(["fetch", "--prune", url, "+refs/heads/*:refs/heads/*"], cwd=str(base_repo))
    else:
        r = await git.run(["clone", "--bare", url, str(base_repo)], cwd=str(ctx.svc.work_root))
    if r.rc != 0:
        return error("clone_failed", r.err[-300:], expected=True)
    r = await git.run(["worktree", "add", "-B", branch, str(path), w["base_ref"]], cwd=str(base_repo))
    if r.rc != 0:
        return error("worktree_failed", r.err[-300:])
    sha = await git.run(["rev-parse", "HEAD"], cwd=str(path))
    return ok({"workdir_id": wd, "branch": branch, "base_sha": sha.out.strip()})
