"""監視（D-7.1、S-8.6）。

- supervisor: 出力の更新時刻（stall_min）・経過（budget_min）・呼び元（orphaned）を見る。
  出力が stall_min 分止まれば stalled、budget_min 超過は timed_out として区別する。
  依頼元の対話終了後も受付済みジョブを継続し、結果を回収する。
- finisher: verify を走らせる → STATUS 行を読む → 結果を保存 → 成績を resources へ → 枠を返す。
  worker-pool が verify を実行し、終了コードを記録する。本人の合格主張は採用しない。
  report.status は ready / blocked / failed / dropped。最終行 STATUS: <値> と、
  blocked 時の次行 HUMAN_ACTION: <利用者に要る操作> を読み取る。
  ジョブが ok となる条件は verify.rc == 0 かつ report.status == ready。
- 起動のし直し: jobs の launching・running を見て、コンテナの名前（ax-job-<id>）で照合する
  （launcher の job-list）。無ければ lost。同じ外部操作を再送しない。
- 各試行に attempt_id、モデル、開始・終了時刻、結果、検証結果、切替理由を対応付ける。
  切替前に前試行の変更を安全に引き継ぐか隔離できることを確認し、できなければ再試行を止めて外へ出す。
- 終了、失敗、取消、時間切れ、主体消失の各経路で試行が保持する枠を一度だけ解放する。
"""
from __future__ import annotations

import re
from typing import Any

from axcommon import timeutil

STATUS_RE = re.compile(r"^STATUS:\s*(ready|blocked|failed|dropped)\s*$", re.M)
HUMAN_RE = re.compile(r"^HUMAN_ACTION:\s*(.+)\s*$", re.M)


def parse_report(text: str | None) -> dict:
    """STATUS 行と HUMAN_ACTION 行を読む（S-8.6）。"""
    if not text:
        return {"status": "failed", "human_action": "", "text": ""}
    lines = [l for l in text.splitlines() if l.strip()]
    m = STATUS_RE.search(text)
    status = m.group(1) if m else "failed"
    human = ""
    if status == "blocked":
        hm = HUMAN_RE.search(text)
        human = hm.group(1) if hm else ""
    return {"status": status, "human_action": human, "text": text}


def is_ok(*, verify_rc: int | None, report: dict) -> bool:
    """ジョブが ok となる条件は verify.rc == 0 かつ report.status == ready（S-8.6）。"""
    return verify_rc == 0 and (report or {}).get("status") == "ready"


def classify_state(*, state: str, wall_s: float | None = None, idle_s: float | None = None,
                   budget_min: int | None = None, stall_min: int | None = None,
                   caller_alive: bool = True, container_exists: bool = True) -> dict:
    """supervisor の判定。区別できる。"""
    if not container_exists:
        return {"state": "lost", "reason": "container_missing"}
    if not caller_alive:
        return {"state": "orphaned", "reason": "caller_ejected_or_cancelled"}
    if budget_min is not None and wall_s is not None and wall_s > budget_min * 60:
        return {"state": "timed_out", "reason": "budget_exceeded"}
    if stall_min is not None and idle_s is not None and idle_s > stall_min * 60:
        return {"state": "stalled", "reason": "output_stalled"}
    return {"state": state, "reason": "running"}


def error_class_of(*, http_status: int | None = None, api_error_status: int | None = None,
                   exit_code: int | None = None) -> str:
    """S-8.6 の利用制限の区別。429 は system.expected（rate_limit）。"""
    if api_error_status == 429 or http_status == 429:
        return "rate_limit"
    if api_error_status in (401, 403) or http_status == 401:
        return "auth"
    if exit_code not in (None, 0):
        return "exit"
    return "ok"


def counts_as_failure(error_class: str, *, system_expected: bool = False) -> str:
    """利用制限・quota・接続障害・保留は能力上の失敗に数えない（S-8.5）。"""
    if error_class in ("rate_limit", "quota", "network", "hold"):
        return "hold"
    if system_expected or error_class == "expected":
        return "no"
    return "yes"
