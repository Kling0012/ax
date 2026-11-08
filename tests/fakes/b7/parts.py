"""B7 の偽の部品の本体。"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable

from axcommon.client import ApiError, Unavailable
from runtime.subloop.ctx import CmdResult


class FakeSink:
    """EventSink の偽。emit した事象を events に溜める。"""

    def __init__(self):
        self.events: list[dict] = []

    def emit(self, kind, subject, data=None, **kw):
        ev = {"kind": kind, "subject": subject, "data": copy.deepcopy(data or {}), **kw}
        self.events.append(ev)
        return ev

    def of(self, kind: str, **match) -> list[dict]:
        out = [e for e in self.events if e["kind"] == kind]
        for k, v in match.items():
            out = [e for e in out if e["data"].get(k) == v or e.get(k) == v]
        return out

    def flush(self):
        return 0


class FakeLedger:
    def __init__(self):
        self.todos: dict[str, dict] = {}
        self.created: list[dict] = []
        self.transitions: list[tuple] = []
        self._n = 0

    def create_todo(self, todo: dict):
        t = dict(todo)
        if "id" not in t:
            self._n += 1
            t["id"] = f"todo-{self._n}"
        if t["id"] in self.todos:
            raise ApiError(409, "conflict")
        self.todos[t["id"]] = t
        self.created.append(copy.deepcopy(t))
        return t

    def get_todo(self, todo_id: str):
        if todo_id not in self.todos:
            raise ApiError(404, "not_found")
        return self.todos[todo_id]

    def list_todos(self, **params):
        return list(self.todos.values())

    def transition(self, *a, **k):  # サブループは呼べない（S-7.10）。呼ばれたら試験が落ちる
        self.transitions.append((a, k))
        raise AssertionError("サブループが transition を呼んだ")

    answer = transition


class FakeTimeline:
    def __init__(self):
        self.events: list[dict] = []
        self.down = False

    def add(self, ev: dict):
        self.events.append(ev)

    def get_events(self, *, after=None, kind=None, subject=None, **kw):
        if self.down:
            raise Unavailable("down")
        evs = [e for e in self.events if (kind is None or e["kind"] == kind) and (subject is None or e.get("subject") == subject)]
        return {"events": evs[: kw.get("limit") or len(evs)]}


class FakeControl:
    def __init__(self):
        self.eff = {"mode": "run", "knobs": {}, "revision": 0}

    def effective(self, pid):
        return self.eff

    def plugins(self):
        return []


class FakePool:
    """worker-pool の偽。submit したジョブを submitted に残し、結果は役割ごとの script（キュー）か default から返す。

    pool.script("impl", {...}, {...})  役割 impl の次のジョブから順に返す結果（最後の物は繰り返さず default に戻る）
    pool.effects["impl"] = fn(body, workdir_path)  ジョブの副作用（ファイルの書き換えなど）。submit のときに実行する
    結果の形は job.schema.json。省略した項目は default（state ok、report ready、verify.rc 0）で埋める。
    """

    def __init__(self, work_root: Path | None = None):
        self.submitted: list[dict] = []
        self.jobs: dict[str, dict] = {}
        self.scripts: dict[str, list[dict]] = {}
        self.effects: dict[str, Callable] = {}
        self.work_root = work_root
        self.reject_next: list[tuple[int, str]] = []
        self.cancelled: list[str] = []
        self._n = 0
        self.down = False
        self.polls: dict[str, int] = {}
        self.pending_polls = 0     # 最初の何回かの job() は running を返す

    def script(self, role: str, *results: dict):
        self.scripts.setdefault(role, []).extend(results)

    def default_result(self, body: dict) -> dict:
        r: dict[str, Any] = {"state": "ok", "model": f"fake-{body['role']}", "role": body["role"], "report": {"status": "ready", "text": ""},
                             "verify": {"rc": 0, "log_tail": ""}, "ended_at": "2026-11-05T12:00:00.000Z"}
        if body.get("workdir_id"):
            r["workdir_id"] = body["workdir_id"]
        return r

    def submit(self, body: dict):
        if self.down:
            raise Unavailable("down")
        if self.reject_next:
            st, code = self.reject_next.pop(0)
            raise ApiError(st, code)
        self._n += 1
        jid = f"job-{self._n}"
        self.submitted.append(copy.deepcopy(body))
        res = self.default_result(body)
        q = self.scripts.get(body["role"])
        if q:
            res.update(q.pop(0))
        res["id"] = jid
        res.setdefault("role", body["role"])
        self.jobs[jid] = res
        fx = self.effects.get(body["role"])
        if fx is not None:
            fx(body, (Path(self.work_root) / f"job-{body['workdir_id']}") if body.get("workdir_id") and self.work_root else None)
        return {"id": jid, "state": "queued"}

    def job(self, job_id: str):
        if self.down:
            raise Unavailable("down")
        if job_id not in self.jobs:
            raise ApiError(404, "not_found")
        self.polls[job_id] = self.polls.get(job_id, 0) + 1
        if self.polls[job_id] <= self.pending_polls:
            return {"id": job_id, "state": "running", "role": self.jobs[job_id]["role"]}
        return self.jobs[job_id]

    def cancel(self, job_id: str):
        self.cancelled.append(job_id)
        return {}

    def by_role(self, role: str) -> list[dict]:
        return [b for b in self.submitted if b["role"] == role]


class FakeGitHub:
    def __init__(self):
        self.issues: dict[str, list[dict]] = {}
        self.prs: dict[str, dict[int, dict]] = {}
        self.comments: dict[tuple, list[dict]] = {}
        self.posted: list[tuple] = []         # 外への操作の記録（二重実行の検査）
        self.labels_of: dict[tuple, set] = {}
        self.runs: list[dict] = []
        self.dispatched: list[tuple] = []
        self.fail: list[Exception] = []
        self._cid = 0

    def _maybe_fail(self):
        if self.fail:
            raise self.fail.pop(0)

    def list_issues(self, repo, *, label=None, state="open", limit=50):
        self._maybe_fail()
        return list(self.issues.get(repo, []))[:limit]

    def list_prs(self, repo, *, state="open", base=None, limit=50):
        self._maybe_fail()
        return list(self.prs.get(repo, {}).values())[:limit]

    def get_pr(self, repo, number):
        self._maybe_fail()
        return self.prs[repo][number]

    def find_pr(self, repo, head):
        self._maybe_fail()
        for p in self.prs.get(repo, {}).values():
            if (p.get("head") or {}).get("ref") == head:
                return p
        return None

    def list_comments(self, repo, number):
        self._maybe_fail()
        return list(self.comments.get((repo, number), []))

    def comment(self, repo, number, body):
        self._maybe_fail()
        self._cid += 1
        c = {"id": self._cid, "body": body}
        self.comments.setdefault((repo, number), []).append(c)
        self.posted.append(("comment", repo, number))
        return c

    def pr_open(self, repo, *, head, base, title, body=""):
        self._maybe_fail()
        n = len(self.prs.setdefault(repo, {})) + 1
        p = {"number": n, "html_url": f"https://example.invalid/{repo}/pull/{n}", "head": {"ref": head, "sha": "a" * 40}, "base": {"ref": base}, "merged": False}
        self.prs[repo][n] = p
        self.posted.append(("pr_open", repo, head))
        return p

    def merge(self, repo, number, *, sha=None, method="squash"):
        self._maybe_fail()
        self.prs[repo][number]["merged"] = True
        self.posted.append(("merge", repo, number))
        return {"sha": "b" * 40}

    def labels(self, repo, number):
        return sorted(self.labels_of.get((repo, number), set()))

    def add_labels(self, repo, number, labels):
        self.labels_of.setdefault((repo, number), set()).update(labels)
        self.posted.append(("label", repo, number))

    def dispatch_workflow(self, repo, workflow, ref, inputs):
        self.dispatched.append((repo, workflow, ref, dict(inputs)))
        self.runs.append({"id": 900 + len(self.runs), "display_title": inputs.get("ax_marker", ""), "status": "completed", "conclusion": "success"})

    def list_runs(self, repo, *, event=None):
        return list(self.runs)

    def get_run(self, repo, run_id):
        for r in self.runs:
            if r["id"] == run_id:
                return r
        raise ApiError(404, "not_found")


class FakeGit:
    """git の偽。worktree add は実際にディレクトリ（と .git ファイル）を作る。push は remotes に記録する。"""

    def __init__(self):
        self.calls: list[tuple[list[str], str | None]] = []
        self.remote_heads: dict[str, dict[str, str]] = {}   # url → {branch: sha}
        self.dirty: dict[str, bool] = {}
        self.head_sha = "1" * 40
        self.fail_on: dict[str, CmdResult] = {}
        self._n = 0

    async def run(self, args, cwd=None, timeout_s=None):
        self.calls.append((list(args), cwd))
        key = args[0] if args[0] != "-c" else next(a for a in args if a in ("commit", "push", "add"))
        if key in self.fail_on:
            return self.fail_on[key]
        if args[0] == "clone":
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
            (Path(args[-1]) / "HEAD").write_text("ref: refs/heads/main\n")
            return CmdResult(0)
        if args[0] == "fetch":
            return CmdResult(0)
        if args[0] == "worktree":
            p = Path(args[args.index("-B") + 2])
            p.mkdir(parents=True, exist_ok=True)
            (p / ".git").write_text("gitdir: fake\n")
            return CmdResult(0)
        if args[0] == "rev-parse":
            return CmdResult(0, self.head_sha + "\n")
        if args[0] == "merge-base":
            return CmdResult(0, self.head_sha + "\n")
        if args[0] == "status":
            return CmdResult(0, " M x\n" if self.dirty.get(cwd or "", False) else "")
        if args[0] == "add":
            return CmdResult(0)
        if args[0] == "diff":
            return CmdResult(1 if self.dirty.get(cwd or "", False) else 0)
        if "commit" in args:
            self._n += 1
            self.head_sha = f"{self._n:040x}"
            self.dirty[cwd or ""] = False
            return CmdResult(0)
        if args[0] == "push":
            url, refspec = args[1], args[2]
            branch = refspec.split("refs/heads/", 1)[1]
            self.remote_heads.setdefault(url, {})[branch] = self.head_sha
            return CmdResult(0)
        if args[0] == "ls-remote":
            url, ref = args[1], args[2]
            sha = self.remote_heads.get(url, {}).get(ref.replace("refs/heads/", ""))
            return CmdResult(0, f"{sha}\t{ref}\n" if sha else "")
        return CmdResult(0)

    def pushes(self) -> list[tuple[list[str], str | None]]:
        return [c for c in self.calls if c[0][0] == "push"]


class FakeShell:
    def __init__(self):
        self.calls: list[tuple] = []
        self.results: list[CmdResult] = []

    async def run(self, cmd, cwd=None, timeout_s=300.0):
        self.calls.append((cmd, cwd))
        return self.results.pop(0) if self.results else CmdResult(0, "ok\n")


class FakeResources:
    """resources の口の偽（env ジョブ）。post で受け、get で状態を返す。"""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.posted: list[dict] = []
        self.state = "succeeded"
        self.artifacts: list[dict] = [{"name": "state.json", "size": 2}]
        self.cancelled: list[str] = []

    def post(self, path, body=None, **kw):
        if path == "/v1/env/jobs":
            if body["job_id"] in self.jobs:
                raise ApiError(409, "conflict")
            self.posted.append(copy.deepcopy(body))
            self.jobs[body["job_id"]] = {"job_id": body["job_id"], "state": self.state, "env_id": "fake-env"}
            return {"job_id": body["job_id"], "selected": "fake-env"}
        if path.endswith("/cancel"):
            jid = path.split("/")[-2]
            if jid not in self.jobs:
                raise ApiError(404, "not_found")
            self.cancelled.append(jid)
            return {}
        raise ApiError(404, "not_found")

    def get(self, path, **kw):
        if path.endswith("/artifacts"):
            return self.artifacts
        jid = path.rsplit("/", 1)[-1]
        if jid not in self.jobs:
            raise ApiError(404, "not_found")
        return self.jobs[jid]
