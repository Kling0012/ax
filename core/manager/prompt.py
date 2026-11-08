"""LLM へ渡す依頼文（D-9.1）。事象の要約、許された操作の一覧、生きている命令、プラグインの knob と capabilities、資源の状態。
秘密は渡さない（呼び出し側が redact を通す）。返答は S-10.3 の JSON Schema に合う JSON オブジェクト 1 つ。"""
from __future__ import annotations

import json

OUTPUT_RULE = (
    "## 出力の形\n"
    "次の形の JSON オブジェクトを 1 つだけ、最後に書く。形に合わない返答は捨てられ、事象は台帳へ出される。\n"
    '{"actions": [ {"type": "control.issue", "command": {"target": "…", "op": "…", "args": {}, "expires_at": "…", "on_expiry": "ask_issuer"}, "reason": "…"},\n'
    '  {"type": "control.release", "command_id": "…", "reason": "…"},\n'
    '  {"type": "control.renew", "command_id": "…", "expires_at": "…", "reason": "…"},\n'
    '  {"type": "ledger.eject", "subject": "<事象の ID>", "reason": "…", "check_at": "…"},\n'
    '  {"type": "ledger.add", "todo": {"title": "…"}, "reason": "…"},\n'
    '  {"type": "timer.create", "timer": {"action": "ledger.recheck|control.release|plugin.run_now", "at": "…", "reason": "…"}, "reason": "…"},\n'
    '  {"type": "noop", "reason": "…"} ]}\n'
)

RULES = (
    "## 守ること\n"
    "- してよい: 1 つのプラグインへの期限付きの制御命令、資源の除外（期限付き。expires_at と args.until を同じにし on_expiry は release）、"
    "タイマーの作成、ToDo の作成・台帳へ出すこと、ループ化の提案カード。\n"
    "- してはいけない: 中核の部品の制御、調整項目の範囲や上限の値の変更、サブループの作成、waiting_user の件への代理の回答、戻しにくい操作。\n"
    "- 命令の期限は今から {lease} 分以内。判断できない事象は ledger.eject で理由と check_at を付けて台帳へ出す。\n"
    "- resource.repeat_failure は、先にその資源を exclude_resource で期限付きで外す。恒久修繕の ToDo は管理役が別の段で作る。\n"
    "- 秘密や個人情報を書かない。\n"
)


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


def build_inbox_prompt(*, now: str, events: list[dict], parked_due: list[dict], plugins: list[dict], commands: list[dict],
                       resources: list[dict], lease_min: float) -> str:
    parts = ["あなたは AX の管理役。時系列記録の事象を読み、許された操作だけを返す。", RULES.format(lease=int(lease_min)),
             f"## 今の時刻\n{now}"]
    parts.append("## 事象（種類ごと。古い順）\n" + _j(events))
    if parked_due:
        parts.append("## check_at を過ぎた parked の ToDo（見直す）\n" + _j(parked_due))
    parts.append("## プラグイン（mode・capabilities・knob）\n" + _j(plugins))
    parts.append("## 生きている命令（issued_by が発行者）\n" + _j(commands))
    if resources:
        parts.append("## 資源の状態\n" + _j(resources))
    parts.append(OUTPUT_RULE)
    return "\n\n".join(parts)


def build_commands_prompt(*, now: str, due: list[dict], lease_min: float, max_renewals: int) -> str:
    parts = ["あなたは AX の管理役。自分が出した制御命令のうち、期限切れ（expired_pending）か期限が近い物を、解除するか延長するか決める。",
             f"## 今の時刻\n{now}",
             f"## 規則\n- 解除は control.release、延長は control.renew（新しい期限は今から {int(lease_min)} 分以内。1 つの命令につき {max_renewals} 回まで）。\n"
             "- この段で使える type は control.release、control.renew、noop だけ。\n- 迷ったら解除する（命令は緩和策で、元の設定へ戻すのが安全）。",
             "## 対象の命令\n" + _j(due), OUTPUT_RULE]
    return "\n\n".join(parts)


def build_review_prompt(*, now: str, inspection_id: str, report: dict, lease_min: float) -> str:
    parts = ["あなたは AX の管理役。定期点検の見直し（段 4〜7）。集計した使用量と実績を読み、最適化の提案を ToDo か提案カードで返す。",
             RULES.format(lease=int(lease_min)),
             f"## 今の時刻\n{now}\n点検 ID: {inspection_id}",
             "- priority_repairs にある未完了の予備役の根本原因を最優先で扱う。既存 ToDo を重複して作らない。",
             "## 集計（系統別の使用量の推移、GPT・Claude を選んだ理由の内訳、除外が続いたモデル）\n" + _j(report),
             "- モデル選択の設定変更は利用者が行う。提案は ledger.add の提案カード（waiting_user。理由は preference か range_review）で出す。\n"
             "- 退役の提案も同じ形のカードにする。", OUTPUT_RULE]
    return "\n\n".join(parts)
