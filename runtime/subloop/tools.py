"""外のプログラムとの接点: git、シェル、GitHub。ノードはこれを `ctx.svc.git` / `ctx.svc.shell` / `ctx.svc.github` 経由で使う。
試験は tests/fakes/b7 の偽を差し込む（ネットワークとサービスを使わない）。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from axcommon.client import ApiClient

from .ctx import CmdResult

CLEAN_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")


def _env() -> dict[str, str]:
    e = {k: os.environ[k] for k in CLEAN_ENV_KEYS if k in os.environ}
    e["GIT_TERMINAL_PROMPT"] = "0"
    return e


async def _exec(argv: list[str], cwd: str | None, timeout_s: float, input_: bytes | None = None) -> CmdResult:
    proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=_env(), stdin=asyncio.subprocess.PIPE if input_ is not None else asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(input_), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return CmdResult(124, "", "timeout")
    return CmdResult(proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


class GitRunner:
    """`git` を引数の配列で実行する（シェルを通さない）。"""

    def __init__(self, timeout_s: float = 300.0):
        self.timeout_s = timeout_s

    async def run(self, args: list[str], cwd: str | None = None, timeout_s: float | None = None) -> CmdResult:
        return await _exec(["git", *args], cwd, timeout_s or self.timeout_s)


class ShellRunner:
    """コンテナの中でコマンドを 1 つ走らせる（shell.run、ci.collect など）。`sh -c`。"""

    async def run(self, cmd: str, cwd: str | None = None, timeout_s: float = 300.0) -> CmdResult:
        return await _exec(["sh", "-c", cmd], cwd, timeout_s)


class GitHubClient(ApiClient):
    """GitHub の REST API の薄い窓口。トークンは秘密 github-token（compose の secrets）から読む。値をログに出さない。"""

    def __init__(self, token: str, base_url: str | None = None, **kw: Any):
        super().__init__(base_url or os.environ.get("AX_GITHUB_API_URL") or "https://api.github.com", token,
                         extra_headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, **kw)

    def list_issues(self, repo: str, *, label: str | None = None, state: str = "open", limit: int = 50) -> list[dict]:
        rows = self.get(f"/repos/{repo}/issues", params={"state": state, "labels": label, "per_page": min(limit, 100)}) or []
        return [r for r in rows if "pull_request" not in r][:limit]

    def list_prs(self, repo: str, *, state: str = "open", base: str | None = None, limit: int = 50) -> list[dict]:
        return (self.get(f"/repos/{repo}/pulls", params={"state": state, "base": base, "per_page": min(limit, 100)}) or [])[:limit]

    def get_pr(self, repo: str, number: int) -> dict:
        return self.get(f"/repos/{repo}/pulls/{number}")

    def find_pr(self, repo: str, head: str) -> dict | None:
        owner = repo.split("/")[0]
        rows = self.get(f"/repos/{repo}/pulls", params={"head": f"{owner}:{head}", "state": "all", "per_page": 5}) or []
        return rows[0] if rows else None

    def list_comments(self, repo: str, number: int) -> list[dict]:
        return self.get(f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100}) or []

    def comment(self, repo: str, number: int, body: str) -> dict:
        return self.post(f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def pr_open(self, repo: str, *, head: str, base: str, title: str, body: str = "") -> dict:
        return self.post(f"/repos/{repo}/pulls", {"head": head, "base": base, "title": title, "body": body})

    def merge(self, repo: str, number: int, *, sha: str | None = None, method: str = "squash") -> dict:
        b: dict[str, Any] = {"merge_method": method}
        if sha:
            b["sha"] = sha
        return self.put(f"/repos/{repo}/pulls/{number}/merge", b)

    def labels(self, repo: str, number: int) -> list[str]:
        return [x["name"] for x in (self.get(f"/repos/{repo}/issues/{number}/labels") or [])]

    def add_labels(self, repo: str, number: int, labels: list[str]) -> Any:
        return self.post(f"/repos/{repo}/issues/{number}/labels", {"labels": labels})

    def dispatch_workflow(self, repo: str, workflow: str, ref: str, inputs: dict) -> Any:
        return self.request("POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches", json_body={"ref": ref, "inputs": inputs}, ok_statuses=(204,))

    def list_runs(self, repo: str, *, event: str | None = None) -> list[dict]:
        r = self.get(f"/repos/{repo}/actions/runs", params={"event": event, "per_page": 30}) or {}
        return r.get("workflow_runs", [])

    def get_run(self, repo: str, run_id: int) -> dict:
        return self.get(f"/repos/{repo}/actions/runs/{run_id}")
