"""部品庫の型が共有する処理（worker-pool のジョブを出して待つ、テストの出力の読み取り、鍵の作り方など）。"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from axcommon import timeutil
from axcommon.client import ApiError, Unavailable

TERMINAL_JOB_STATES = ("ok", "failed", "timed_out", "cancelled", "lost", "rejected", "parked", "skipped", "orphaned")
WRITE_ROLES = ("test_design", "impl", "impl_hard")
WORKDIR_ROLES = WRITE_ROLES + ("review", "verify")
WAITING_REASONS = ("physical", "secret_input", "money", "publish", "irreversible", "preference", "range_review")


def short_hash(*parts: str, n: int = 12) -> str:
    h = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return h[:n]


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "x")[:n].strip("-") or "x"


async def wait_job(ctx: Any, job_id: str) -> dict:
    """worker-pool の GET /v1/jobs/{id} を終わりまで引く。間隔は svc.poll_s。打ち切りは ctx.deadline（超えたら最後の応答を返す）。
    実行系の timeout_s（asyncio.wait_for）が先に切ることもある。接続できない間は待ち続ける（deadline まで）。"""
    deadline = timeutil.parse_ts(ctx.deadline)
    last: dict = {"id": job_id, "state": "queued"}
    while True:
        try:
            j = await ctx.call(ctx.svc.pool.job, job_id)
            if isinstance(j, dict):
                last = j
                if j.get("state") in TERMINAL_JOB_STATES:
                    return j
        except (Unavailable, ApiError):
            pass
        if timeutil.now() >= deadline:
            return last
        await ctx.sleep(ctx.svc.poll_s)


async def submit_job(ctx: Any, body: dict) -> str | dict:
    """POST /v1/jobs。成功なら job_id、拒否（4xx）なら {"rejected": 内容}。接続できない・5xx は例外のまま（実行系が再試行する）。"""
    try:
        r = await ctx.call(ctx.svc.pool.submit, body)
    except ApiError as e:
        return {"rejected": {"status": e.status, "code": e.code, "message": e.args[0] if e.args else ""}}
    jid = (r or {}).get("id") or (r or {}).get("job_id")
    if not jid:
        return {"rejected": {"status": 0, "code": "no_job_id", "message": "応答に job の ID が無い"}}
    return jid


_PYTEST_SUMMARY = re.compile(r"(?:(\d+) failed)?[, ]*(?:(\d+) passed)?[, ]*(?:(\d+) errors?)?", re.I)


def parse_test_counts(log: str) -> dict[str, int | None]:
    """pytest / jest / dotnet の要約行から {passed, failed, errors} を読む。読めなければ値は None。"""
    out: dict[str, int | None] = {"passed": None, "failed": None, "errors": None}
    m = re.search(r"=+ ([^=\n]*(?:passed|failed|error)[^=\n]*) in [\d.]+s", log)
    if m:
        for key, pat in (("passed", r"(\d+) passed"), ("failed", r"(\d+) failed"), ("errors", r"(\d+) errors?")):
            mm = re.search(pat, m.group(1))
            out[key] = int(mm.group(1)) if mm else 0
        return out
    m = re.search(r"Tests:\s+(?:(\d+) failed,\s*)?(?:(\d+) passed,\s*)?(\d+) total", log)  # jest
    if m:
        out.update(failed=int(m.group(1) or 0), passed=int(m.group(2) or 0), errors=0)
        return out
    m = re.search(r"(Passed|Failed)!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+)", log)  # dotnet test
    if m:
        out.update(failed=int(m.group(2)), passed=int(m.group(3)), errors=0)
        return out
    return out


INVALID_RED = re.compile(r"ImportError|ModuleNotFoundError|SyntaxError|IndentationError|error during collection|Cannot find module|error CS\d+|error TS\d+", re.I)


def api_error_out(e: ApiError) -> dict:
    """GitHub など外の API の 4xx を、ノードの error 出力にする。429・408 は再試行の対象（system.expected）。"""
    from .nodeapi import error

    return error(f"http_{e.status}", f"{e.code}", expected=e.status in (408, 429))


async def adopt_or_submit(ctx: Any, body: dict) -> tuple[str | None, dict | None]:
    """worker-pool にジョブを出す。落ちて中断した前の試行が同じノードにあり、そのジョブがまだ生きているか成功していれば、新しく出さずに引き継ぐ。
    戻り: (job_id, 拒否の内容)。拒否なら job_id は None。"""
    from .nodeapi import DEFAULT_META  # noqa: F401

    for prev in reversed(ctx.previous_runs()):
        ref = prev.get("external_ref") or {}
        if prev["state"] == "interrupted" and ref.get("job_id"):
            try:
                j = await ctx.call(ctx.svc.pool.job, ref["job_id"])
            except (ApiError, Unavailable):
                break
            if isinstance(j, dict) and j.get("state") not in ("failed", "timed_out", "cancelled", "lost", "rejected", None):
                ctx.set_ref({"job_id": ref["job_id"], "adopted": True})
                return ref["job_id"], None
            break
    r = await submit_job(ctx, body)
    if isinstance(r, dict):
        return None, r["rejected"]
    ctx.set_ref({"job_id": r})
    return r, None


def job_view(job: dict) -> dict:
    rep = job.get("report") or {}
    ver = job.get("verify") or {}
    return {"state": job.get("state"), "report_status": rep.get("status"), "human_action": rep.get("human_action"), "report_text": rep.get("text", ""),
            "verify_rc": ver.get("rc"), "log_tail": ver.get("log_tail", ""), "model": job.get("model"), "workdir_id": job.get("workdir_id"),
            "ended_at": job.get("ended_at"), "changed_files": job.get("changed_files") or []}


async def run_verify_job(ctx: Any, cmd: str, workdir_id: str, *, label: str = "verify") -> tuple[dict | None, dict | None]:
    """役割 verify のジョブ（runner は verify-<言語>。S-8.6、F-19）を、同じ workdir_id で出して待つ。
    戻り: (job_view, エラーの出力)。エラーは出力の辞書（そのまま return してよい）。"""
    from .nodeapi import error

    body = {"role": "verify", "prompt": f"{label}: 検証のコマンドを実行する", "verify": cmd, "workdir_id": workdir_id,
            "sensitivity": ctx.manifest.get("sensitivity", "private"), "project": (ctx.item or {}).get("project"), "subloop": ctx.svc.plugin_id,
            "item_id": ctx.item_id, "todo_id": (ctx.item or {}).get("todo_id"), "budget_min": max(1, int(ctx.knobs.get("verify_budget_min", 30)))}
    body = {k: v for k, v in body.items() if v is not None}
    jid, rej = await adopt_or_submit(ctx, body)
    if jid is None:
        return None, error("job_rejected", f"{rej.get('code')}", expected=rej.get("status") in (408, 429, 503))
    job = await wait_job(ctx, jid)
    v = job_view(job)
    v["job_id"] = jid
    st = v["state"]
    if st not in ("ok", "failed"):
        return v, error(f"job_{st}", f"verify のジョブが {st}", expected=st in ("timed_out", "queued", "launching", "submitted", "running", "stalled"))
    return v, None


def snapshot_tree(root: Any, rel_dir: str) -> dict[str, str]:
    """root/rel_dir の下の通常ファイルの内容ハッシュ {相対パス: sha256}。root の外へ出るパス・シンボリックリンクはたどらない。"""
    from pathlib import Path

    base = Path(root).resolve()
    top = (base / rel_dir).resolve()
    out: dict[str, str] = {}
    if base != top and base not in top.parents:
        return out
    if not top.is_dir():
        return out
    for p in sorted(top.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        rp = p.resolve()
        if base not in rp.parents:
            continue
        out[str(p.relative_to(base))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out
