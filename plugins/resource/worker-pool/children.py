"""子エージェント（S-8.9、T-47、R-N31〜R-N37）。

- 親の依頼が子を許す場合、親より能力段階が低い適格モデルに空きがあれば子に枠を貸し出せる。
  安全属性、通信制限、情報区分を親から継承する。貸出不能時は理由を返し、親を止めない。
  終了・失敗・取消・時間切れ・消失のいずれも子の枠だけを解放する。
- 完了した子の結果を自動評価し、pass / fail / pending を親の検証結果と別に記録する。
  曖昧なら pending。利用制限・接続障害は能力上の失敗に数えず、会話全文を評価記録に複製しない。
- 規則違反の検知は S-3 の事象と AX の通知経路へ渡す。週 1 回、前回監査以降の子評価から
  10% を無作為抽出し（端数は切り上げ）、対象が無ければ 0 件と記録する。監査結果は元の子評価と区別する。
"""
from __future__ import annotations

import math
from typing import Any

from core.resources.catalog import LEVEL_ORDER


def _rank(level: str) -> int:
    return LEVEL_ORDER.get(level, -1)


def lendable_slots(*, parent_level: str, candidates: list[dict], jobs: list[dict]) -> dict:
    """親より低い段階のモデルに空きがあれば貸し出せる（T-47）。"""
    options = []
    for c in candidates:
        if _rank(c.get("level", "L1")) >= _rank(parent_level):
            continue
        if c.get("status") not in ("active", "degraded"):
            continue
        cap = c.get("cap")
        used = sum(1 for j in jobs if j.get("model") == c["id"] and j.get("state") in ("launching", "submitted", "running"))
        if cap is not None and used >= cap:
            continue
        options.append(c["id"])
    return {"ok": bool(options), "options": options,
            "reason": "lent" if options else "no_lower_slot"}


def evaluate_child(*, verify_rc: int | None, tool_ok: bool | None = None,
                   rate_limited: bool = False, ambiguous: bool | None = None) -> dict:
    """子の自動評価。利用制限・接続障害は能力上の失敗に数えない。曖昧なら pending。"""
    if rate_limited:
        return {"verdict": "pending", "reason": "rate_limited_not_counted"}
    if ambiguous:
        return {"verdict": "pending", "reason": "ambiguous"}
    if verify_rc == 0 and tool_ok is not False:
        return {"verdict": "pass", "reason": "verify_ok"}
    if verify_rc is None:
        return {"verdict": "pending", "reason": "verify_unknown"}
    return {"verdict": "fail", "reason": f"verify_rc_{verify_rc}"}


def child_event_data(*, parent_job_id: str, child_id: str, model: str, verdict: dict,
                     record_ref: str = "") -> dict:
    """worker.child_evaluation の data。会話全文は入れない。"""
    return {"parent_job_id": parent_job_id, "child_id": child_id, "model": model,
            "verdict": verdict.get("verdict"), "reason": verdict.get("reason"),
            "record_ref": record_ref}


def audit_sample(evaluations: list[dict]) -> dict:
    """週 1 回の抽出監査（S-8.9）。端数は切り上げ。対象が無ければ 0 件と記録する。"""
    n = len(evaluations)
    if n == 0:
        return {"sampled": 0, "total": 0}
    k = math.ceil(n * 0.1)
    # 無作為は固定のシードでは行わない。ここでは件数だけ決め、抜き方を呼び出し側で決める
    return {"sampled": k, "total": n}
