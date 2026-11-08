"""してよいこと・いけないこと（S-10.4）を機械的に照らす 1 つの関数（D-9.1 最後の項）。

check_action(action, ctx) -> None（許す）| Violation(code, message)

許す（S-10.4）: 1 プラグインへの制御命令（期限付き）、資源の除外（期限付き）、タイマーの作成、ToDo の作成・外へ出す、ループ化の提案カード。
禁止（S-10.4）: 中核の制御、調整項目の範囲の変更・上限の値の変更（点検の外）、サブループの作成、waiting_user の件への代理の回答、戻しにくい操作。

JSON Schema（schemas/ext/b9/manager-decision.schema.json）に合わない返答は、ここへ来る前に捨てる（サブループの作成のような
未知の type はその時点で捨てられる）。ここは「形は合うが許されない」操作を 1 件ずつ拒否する。
制御 API も同じ検査を持つ（S-5.4）。これは二重の防御で、拒否は control.rejected を増やさないための先回りでもある。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from axcommon import timeutil

from . import CORE_NAMES

OPS = ("set_mode", "set_knob", "exclude_resource", "include_resource", "cancel", "run_now")
TIMER_ACTIONS = ("ledger.recheck", "control.release", "plugin.run_now")
TODO_STATES_OK = ("todo", "parked", "waiting_user")
TODO_KEYS_OK = {"title", "project", "subloop", "owner", "state", "reason", "depends_on", "check_at", "source", "artifacts", "goal_ids",
                "promise_ids", "due_at", "card"}
# 調整項目の「上限の値」とみなす名前（S-10.4。manifest の knob に印が無いので名前で判定する。DECISIONS）
LIMIT_KNOB_RE = re.compile(r"(^max_|_max$|_cap$|_limit$|^limit|_limits$|^cap_)")
# 範囲そのものを書き換える args のキー（調整項目の範囲の変更）
RANGE_ARG_KEYS = {"min", "max", "range", "bounds", "enum", "limits", "limit"}


@dataclass
class Violation:
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass
class PolicyContext:
    """検査に要る事実。呼び出し側（Manager）が制御 API などから集めて渡す。"""
    plugins: dict[str, dict]                 # 登録済みプラグイン {id: {manifest?: …}}（GET /v1/plugins/{id} の manifest。無ければ空）
    own_commands: dict[str, dict]            # issued_by=manager の命令 {id: command}
    control_max_lease_min: float = 60
    control_max_renewals: int = 3
    timer_max_days: float = 7
    plugins_known: bool = True               # 制御 API に届かず、登録済みの一覧を引けなかったときは False（target の存在は制御 API が検査する）
    now: Any = None                          # datetime。None なら timeutil.now()

    def _now(self):
        return self.now or timeutil.now()


def _ts(text: Any):
    try:
        return timeutil.parse_ts(text)
    except (ValueError, TypeError):
        return None


def _future_within(text: Any, ctx: PolicyContext, max_delta: timedelta) -> Violation | None:
    t = _ts(text)
    if t is None:
        return Violation("bad_time", f"時刻が RFC 3339 でない: {text!r}")
    now = ctx._now()
    if t <= now:
        return Violation("not_future", "期限が未来でない")
    if t - now > max_delta:
        return Violation("lease_too_long", f"期限が長すぎる（上限 {max_delta}）")
    return None


def _knob_of(plugin: dict | None, name: str) -> dict | None:
    manifest = (plugin or {}).get("manifest") or {}
    knobs = manifest.get("knobs") if isinstance(manifest, dict) else None
    return (knobs or {}).get(name) if isinstance(knobs, dict) else None


def check_issue(action: dict, ctx: PolicyContext) -> Violation | None:
    cmd = action["command"]
    target = cmd.get("target")
    if not isinstance(target, str) or target in ("*", "all") or "," in target:
        return Violation("single_target_only", "1 つの命令の対象は 1 プラグイン（S-5.4 の 5）")
    if target in CORE_NAMES:
        return Violation("core_control_forbidden", f"{target} は中核の部品。管理役は制御できない（S-10.4、S-5.1）")
    if ctx.plugins_known and target not in ctx.plugins:
        return Violation("unknown_target", f"{target} は登録済みのプラグインでない（S-5.4 の 1）")
    op, args = cmd.get("op"), cmd.get("args") or {}
    if op not in OPS:
        return Violation("bad_op", f"op {op!r} は S-5.3 の表にない")
    if op == "set_mode" and "permission_ids" in args:
        return Violation("irreversible_forbidden", "送信対象を持つ処理を run にする set_mode は戻しにくい操作（S-10.4、S-23.8）")
    if op == "set_knob":
        name = str(args.get("name", ""))
        if any(k in args for k in RANGE_ARG_KEYS):
            return Violation("range_change_forbidden", "調整項目の範囲の変更は管理役にできない（S-10.4）")
        if LIMIT_KNOB_RE.search(name):
            return Violation("limit_change_forbidden", f"上限の値 {name} の変更は点検の外では管理役にできない（S-10.4）")
        knob = _knob_of(ctx.plugins.get(target), name)
        who = (knob or {}).get("who")
        if who and "manager" not in who:
            return Violation("knob_not_manager", f"調整項目 {name} は manager が変えられない（who={who}）")
    if op in ("exclude_resource", "include_resource") and not args.get("resource_id"):
        return Violation("bad_args", "resource_id が要る")
    # 期限（S-5.4 の 4）。exclude_resource は expires_at == until で on_expiry=release（S-5.4 の 8）
    v = _future_within(cmd.get("expires_at"), ctx, timedelta(minutes=ctx.control_max_lease_min))
    if v:
        return v
    if op == "exclude_resource":
        if args.get("until") != cmd.get("expires_at"):
            return Violation("exclude_until_mismatch", "exclude_resource の until は expires_at と一致させる（S-5.4 の 8）")
        if cmd.get("on_expiry", "release") != "release":
            return Violation("exclude_on_expiry", "exclude_resource の on_expiry は release（S-5.4 の 8）")
    if not (action.get("reason") or cmd.get("reason") or "").strip():
        return Violation("reason_required", "reason が空（S-5.4 の 6）")
    return None


def check_release(action: dict, ctx: PolicyContext) -> Violation | None:
    if action["command_id"] not in ctx.own_commands:
        return Violation("not_own_command", "解除できるのは自分（manager）が出した命令だけ（S-10.2 の 1）")
    return None


def check_renew(action: dict, ctx: PolicyContext) -> Violation | None:
    cmd = ctx.own_commands.get(action["command_id"])
    if cmd is None:
        return Violation("not_own_command", "延長できるのは自分（manager）が出した命令だけ")
    if int(cmd.get("renewals") or 0) >= ctx.control_max_renewals:
        return Violation("renewals_exhausted", f"延長は {ctx.control_max_renewals} 回まで（S-17 control_max_renewals）。解除か、新しい命令にする")
    return _future_within(action.get("expires_at"), ctx, timedelta(minutes=ctx.control_max_lease_min))


def check_eject(action: dict, ctx: PolicyContext) -> Violation | None:
    t = _ts(action.get("check_at"))
    if t is None:
        return Violation("bad_time", "check_at が RFC 3339 でない（S-9.2）")
    if t <= ctx._now():
        return Violation("not_future", "check_at が未来でない（S-9.2）")
    return None


def check_add(action: dict, ctx: PolicyContext) -> Violation | None:
    todo = action["todo"]
    extra = set(todo) - TODO_KEYS_OK
    if extra:
        return Violation("todo_keys", f"管理役が書けない項目: {sorted(extra)}")
    if not isinstance(todo.get("title"), str) or not todo["title"].strip():
        return Violation("title_required", "title が要る")
    state = todo.get("state", "todo")
    if state not in TODO_STATES_OK:
        return Violation("todo_state", f"管理役が作れる state は {TODO_STATES_OK}（running・review・done・dropped は作らない）")
    card = todo.get("card")
    if isinstance(card, dict) and card.get("answer") not in (None, ""):
        return Violation("proxy_answer_forbidden", "waiting_user の件への代理の回答は禁止（S-10.4）")
    if state == "waiting_user":
        if not isinstance(card, dict) or not card.get("question"):
            return Violation("card_required", "waiting_user には質問のカードが要る（S-9.2）")
        if card.get("waiting_reason") not in ("preference", "range_review"):
            return Violation("waiting_reason", "管理役の提案カードの理由は preference か range_review（戻しにくい・公開・支払い・鍵は作らない）")
    if state == "parked":
        if not todo.get("reason") or not todo.get("check_at"):
            return Violation("parked_fields", "parked には reason と check_at が要る（S-9.2）")
    if todo.get("owner", "system") not in ("system", "user"):
        return Violation("owner", "owner は system か user")
    if todo.get("owner") == "user" and state != "waiting_user":
        return Violation("owner", "owner=user は提案カード（waiting_user）のときだけ")
    return None


def check_timer(action: dict, ctx: PolicyContext) -> Violation | None:
    t = action["timer"]
    act = t.get("action")
    if act not in TIMER_ACTIONS:
        return Violation("timer_action", f"タイマーの action は {TIMER_ACTIONS} のどれか（S-7.8）")
    if not (t.get("at") or t.get("every_s")):
        return Violation("timer_when", "at か every_s が要る（S-7.8）")
    if t.get("expires_at"):
        v = _future_within(t["expires_at"], ctx, timedelta(days=ctx.timer_max_days))
        if v:
            return v
    if act == "control.release" and t.get("command_id") not in ctx.own_commands:
        return Violation("not_own_command", "control.release のタイマーが指せるのは自分の命令だけ")
    if act == "plugin.run_now":
        tg = t.get("target")
        if tg in CORE_NAMES or (ctx.plugins_known and tg not in ctx.plugins):
            return Violation("core_control_forbidden" if tg in CORE_NAMES else "unknown_target", "plugin.run_now の対象はプラグインだけ")
    return None


CHECKS = {"control.issue": check_issue, "control.release": check_release, "control.renew": check_renew, "ledger.eject": check_eject,
          "ledger.add": check_add, "timer.create": check_timer, "noop": lambda a, c: None}


def check_action(action: dict, ctx: PolicyContext) -> Violation | None:
    fn = CHECKS.get(action.get("type"))
    if fn is None:
        return Violation("forbidden_action", f"type {action.get('type')!r} は管理役の操作の一覧にない（サブループの作成など。S-10.4）")
    return fn(action, ctx)


def filter_actions(actions: list[dict], ctx: PolicyContext) -> tuple[list[tuple[int, dict]], list[dict]]:
    """(許す操作 [(添字, 操作)], 拒否 [{index, type, code, message}])。許す側は元の順を保つ。"""
    ok, rejected = [], []
    for i, a in enumerate(actions):
        v = check_action(a, ctx)
        if v is None:
            ok.append((i, a))
        else:
            rejected.append({"index": i, "type": a.get("type"), **v.as_dict()})
    return ok, rejected
