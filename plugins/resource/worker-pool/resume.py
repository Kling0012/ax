"""続きからの再開の判定（S-8.12、D-7.5、R-N14、R-N48、T-112、T-114）。

判定の確かめ元は worker-pool 自身の jobs 表の resume_from の行（item_id、model、最後の試行の結果、
終了時刻、last_context、chain_length、root、session_id）で、launcher には確かめさせない。

次を全部満たすときに限る（S-8.12 の 8）:
  (a) 元のジョブが同じ item_id で、同じモデルの最後の試行が ok か、利用者の追加指示による再開。
      failed、timed_out、stalled、lost の会話は続けない。
  (b) 元のジョブの終了から resume_max_age_min 分以内。
  (c) 元のジョブの最後のターンの文脈が resume_max_context_tokens 以下。
      文脈は .usage-<job_id>.json の last_context（最後のターンの値）。取れない（不明）ときは満たさない。
      実行全体の合計は使わない。
  (d) 連なりが resume_max_chain 回以内。
満たさないときは新しい会話で起動し、元の会話を使わない（resume_required でも（b）超過は parked にしない）。
R-N14 の指定があり条件を満たすのに元の会話が見つからないときは、新しい会話に置き換えず parked にする。
"""
from __future__ import annotations

from typing import Any

from axcommon import timeutil
from axcommon.defaults import Defaults

FAILURE_STATES = ("failed", "timed_out", "stalled", "lost")


def _parse(text: str | None) -> Any | None:
    if not text:
        return None
    try:
        return timeutil.parse_ts(text)
    except ValueError:
        return None


def _default(name: str, fallback: Any) -> Any:
    try:
        return Defaults.load().get(name)
    except Exception:  # noqa: BLE001  既定値が読めなくても判定を続ける
        return fallback


def can_resume(original: dict | None, *, item_id: str | None, model: str | None,
               now: str | None = None, age_min: int | None = None,
               max_context: int | None = None, max_chain: int | None = None) -> dict:
    """original は jobs 表の resume_from の行（dict）。不足すれば resume=False と理由を返す。"""
    now = now or timeutil.now_ts()
    am = age_min if age_min is not None else (_default("resume_max_age_min", 55) or 55)
    mc = max_context if max_context is not None else (_default("resume_max_context_tokens", 60000) or 60000)
    mch = max_chain if max_chain is not None else (_default("resume_max_chain", 3) or 3)
    if not original:
        return {"resume": False, "reason": "not_found"}
    if original.get("state") in FAILURE_STATES:
        return {"resume": False, "reason": f"failed_state_{original.get('state')}"}
    if item_id is not None and original.get("item_id") != item_id:
        return {"resume": False, "reason": "item_mismatch"}
    if model is not None and original.get("model") != model:
        return {"resume": False, "reason": "model_mismatch"}
    finished = _parse(original.get("finished_at") or original.get("ended_at"))
    if finished is None:
        return {"resume": False, "reason": "finished_at_unknown"}
    n = _parse(now)
    if n is None:
        return {"resume": False, "reason": "bad_now"}
    if (n - finished).total_seconds() > am * 60:
        return {"resume": False, "reason": "expired"}
    last = original.get("last_context")
    if not isinstance(last, int) or isinstance(last, bool):
        return {"resume": False, "reason": "last_context_unknown"}
    if last > mc:
        return {"resume": False, "reason": "context_too_large"}
    if int(original.get("chain_length") or 0) > mch:
        return {"resume": False, "reason": "chain_too_long"}
    return {"resume": True, "reason": "ok", "root": original.get("root"),
            "session_id": original.get("session_id")}


def decide_job(original: dict | None, *, item_id: str | None, model: str | None,
               resume_required: bool = False, session_found: bool = True, now: str | None = None) -> dict:
    """再開の成否と、resume_required のときの parked の判定（T-112）。

    resume_required が真で (a)〜(d) を満たすのに元の会話の記録が無いときは parked にする。
    それ以外で満たさないときは新しい会話になる（parked にしない）。
    """
    r = can_resume(original, item_id=item_id, model=model, now=now)
    if r["resume"]:
        if not session_found:
            return {"mode": "parked" if resume_required else "new_session", "reason": "session_not_found"}
        return {"mode": "resume", **r}
    if resume_required and r["reason"] == "expired":
        # 利用者の回答が resume_max_age_min を過ぎていれば、新しい会話になる（parked にしない）
        return {"mode": "new_session", **r}
    return {"mode": "new_session", "reason": r["reason"]}


def chain_of(original: dict | None, *, job_id: str) -> dict:
    """元行から新しいジョブに写す値（S-8.12 の 8 の (iii)）。root は job-start へ渡さない。"""
    chain_length = int((original or {}).get("chain_length") or 0)
    return {
        "root": (original or {}).get("root") or job_id,
        "session_id": (original or {}).get("session_id"),
        "chain_length": chain_length + 1 if original else 0,
    }


def root_matches(job_start_root: str | None, jobs_root: str | None) -> bool:
    """job-start が返した root が jobs 表の値と等しいか（root_mismatch）。"""
    return job_start_root == jobs_root
