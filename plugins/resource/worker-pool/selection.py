"""選び方（S-8.4、S-8.3、D-7.4、R-N15、R-N17、R-N18、R-N42、R-N43）。

1. 登録モデルを状態、役割の最低段階、情報区分、画像と用途に必要な能力で絞る。
   `second_opinion` は先に選んだモデルと同じ系統を除く。S-15.1 の安全線は緩めない。
2. Claude は予備役と test_design の Sonnet 5.5 に限る。GPT は難しい用で作業に合う場合、または同じ作業で
   安いモデルが 2 回失敗した場合だけ候補に残す。利用制限・quota・接続障害・保留は
   能力上の失敗に数えない。満枠だけでは失敗回数を増やさない。
3. Jev が適合と判定した候補から最安を選ぶ。Jev が使えないときも適格な候補から最安を選ぶ。
   予備役は例外として Sonnet 5.5 だけを選び、段階・情報区分などの安全線は守る。
   通常選択で同額なら S-8.3 の順、同順位なら ID 昇順とする。
   Jev の点や順序によって高い候補を優先しない。最安の比較は次の順で行う。
   `flat` の候補は追加費用なしとして、tier の順、同じ tier は S-8.3 の順、同順位は ID 昇順で比べる。
   `metered` の候補は単価で比べ、`flat` の候補より後に置く。保留にするのは `metered` で
   単価が未登録の候補だけ。その候補を最安と確定せず、候補から外して理由を記録する。
   定額の候補があれば保留にしない。
4. モデル・系統の同時実行枠を選択直前に再確認し、原子的に枠を確保する。
   最安候補が満枠なら枠を待つ。下位切替は worker_lower_wait_s 以上待ち、許可と実行可能な
   検証があり、安全線を満たす場合に限る。
5. 比率や系統別・日別の費用上限の数値、残量通知は設けない。提供元の quota を上限とする。
6. worker.selection に job_id、候補と除外理由、価格比較に使った情報、枠、選択と理由を記録する。

情報区分は axcommon.policy.allowed で判定し、S-15.1 の安全線を緩めない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from axcommon.policy import TIER_ORDER, allowed

from core.resources.catalog import LEVEL_ORDER
from core.resources.jev import suitable_scores

# この系統のモデルは、S-8.4 の 2 の条件を満たすときだけ通常の候補に残る（GPT・Claude の扱い）。
RESTRICTED_FAMILIES = ("gpt", "claude")

# 予備役（backup_manager）は例外として Sonnet 5.5 だけ（Opus は自動経路に入れない）。
BACKUP_MODEL_ID = "claude/claude-sonnet-5-5"


@dataclass
class Knobs:
    """S-4.3 の調整項目（manifest）。S-17 の既定値を初期値とする。"""
    role_candidates: dict = field(default_factory=dict)
    family_cap: dict = field(default_factory=dict)
    model_cap: dict = field(default_factory=dict)
    jev_enabled: bool = True
    lower_fallback_enabled: bool = True
    lower_fallback_wait_s: int = 300
    promote_after: int = 3
    demote_after: int = 2
    free_success_threshold: float = 0.7
    free_minimum_runs: int = 3
    free_recent_failure_min: int = 60


def _tier_rank(tier: str) -> int:
    return TIER_ORDER.get(tier, 99)


def _level_ok(level: str, min_level: str) -> bool:
    return LEVEL_ORDER.get(level, -1) >= LEVEL_ORDER.get(min_level, 99)


def _policy_allowed(sensitivity: str, model: dict) -> bool:
    return allowed(sensitivity, model)


def _price(model: dict) -> float | None:
    p = model.get("pricing") or {}
    v = p.get("input")
    return float(v) if isinstance(v, (int, float)) else None


def _role_order(role: str | None, model: dict, knobs: Knobs) -> int:
    """S-8.3 の同額時の既定候補の順（小さいほど先）。

    config/roles.yaml の preference（tier の順）で数える。knobs.role_candidates があれば
    そちらを優先し、登録済みのモデル ID の順で数える。
    """
    order: list[str] = list((knobs.role_candidates or {}).get(role, [])) if role else []
    if order:
        return next((i for i, item in enumerate(order) if item in (model["id"], model.get("tier"))),
                    len(order) + _tier_rank(model.get("tier", "standard")))
    return _tier_rank(model.get("tier", "standard"))


def is_restricted(model: dict) -> bool:
    fam = str(model.get("family") or "")
    mid = str(model.get("id") or "")
    return fam in RESTRICTED_FAMILIES or mid.startswith(("codex/", "claude/"))


def restricted_eligible(model: dict, *, job: dict | None = None, cheap_failures: int = 0,
                        hard_use: bool = False, role: str | None = None) -> bool:
    """S-8.4 の 2。難しい用で作業に合う場合、または同じ作業で安いモデルが 2 回失敗した場合だけ。"""
    if role == "backup_manager":
        return (model.get("id") or "") == BACKUP_MODEL_ID
    mid = str(model.get("id") or "")
    if model.get("family") == "claude" or mid.startswith("claude/"):
        return role == "test_design" and mid == BACKUP_MODEL_ID
    if not is_restricted(model):
        return True
    if hard_use:
        return True
    return cheap_failures >= 2


def supports_runner(model: dict, required: str | None) -> bool:
    if required is None:
        return True
    runners = model.get("supported_runners")
    if runners is None or runners == []:
        runners = [model.get("runner") or "agent-cli"]
    return isinstance(runners, list) and required in runners


def select(candidates: list[dict], *, role: str | None = None, min_level: str = "L1",
           sensitivity: str = "public", needs_vision: bool = False, knobs: Knobs | None = None,
           jev_scores: dict[str, float] | None = None, jev_available: bool = False,
           cheap_failures: int = 0, hard_use: bool = False, exclude_ids: set | None = None,
           family_cap: dict | None = None, model_cap: dict | None = None,
           exclude_family: str | None = None, required_runner: str | None = None,
           quota_exhausted_families: set | None = None) -> dict:
    """候補から最安の 1 件を選ぶ。戻り値: {"selected": id か None, "reason": str, "candidates": [...], "excluded": [...]}。

    candidates は登録簿の資源の dict の一覧。除外の理由は除外一覧の中に残す。
    保留（metered で単価未登録だけ、または適格候補が無い）は selected=None で理由を返す。
    """
    knobs = knobs or Knobs()
    _fc = family_cap if family_cap is not None else knobs.family_cap
    _mc = model_cap if model_cap is not None else knobs.model_cap
    excl: list[dict] = []
    pool: list[dict] = []
    for m in candidates:
        mid = m["id"]
        if not supports_runner(m, required_runner):
            excl.append({"id": mid, "reason": "runner_not_supported"})
            continue
        if exclude_family and (not isinstance(m.get("family"), str) or not m["family"].strip() or m["family"] == exclude_family):
            excl.append({"id": mid, "reason": "second_opinion_same_or_unknown_family"})
            continue
        if exclude_ids and mid in exclude_ids:
            excl.append({"id": mid, "reason": "second_opinion_same_family"})
            continue
        if role == "backup_manager":
            if mid != BACKUP_MODEL_ID:
                excl.append({"id": mid, "reason": "backup_manager_only_sonnet55"})
                continue
        if role == "manager" and is_restricted(m):
            excl.append({"id": mid, "reason": "manager_gpt_claude_forbidden"})
            continue
        if role == "manager" and not (
            (m.get("tier") == "standard" and m.get("family") == "glm")
            or (m.get("tier") == "flash" and str(m.get("family", "")).startswith("deepseek"))
            or m.get("tier") == "free"
        ):
            excl.append({"id": mid, "reason": "manager_family_not_eligible"})
            continue
        if m.get("kind") != "model":
            excl.append({"id": mid, "reason": "not_model"})
            continue
        if m.get("status") not in ("active", "degraded"):
            excl.append({"id": mid, "reason": f"status_{m.get('status')}"})
            continue
        if m.get("kind") == "environment":
            excl.append({"id": mid, "reason": "kind_environment"})
            continue
        if role != "manager" and not _level_ok(m.get("level", "L1"), min_level):
            excl.append({"id": mid, "reason": "level_too_low"})
            continue
        if not _policy_allowed(sensitivity, m):
            excl.append({"id": mid, "reason": "sensitivity_denied"})
            continue
        if needs_vision and not m.get("vision"):
            excl.append({"id": mid, "reason": "vision_not_available"})
            continue
        if not restricted_eligible(m, job=None, cheap_failures=cheap_failures, hard_use=hard_use, role=role):
            excl.append({"id": mid, "reason": "restricted_no_second_condition"})
            continue
        if quota_exhausted_families and m.get("family") in quota_exhausted_families:
            excl.append({"id": mid, "reason": "quota_exhausted"})
            continue
        pool.append(m)
    if not pool:
        return {"selected": None, "reason": "no_eligible_candidates", "candidates": [], "excluded": excl, "jev_used": bool(jev_available)}

    # Jev が適合と判定した候補だけに絞る（使えないときは適格な候補から選ぶ。S-8.4 の 3）
    if knobs.jev_enabled and jev_available and jev_scores:
        suitable = suitable_scores(jev_scores)
        if suitable:
            pool = [m for m in pool if m["id"] in suitable]
            if not pool:
                return {"selected": None, "reason": "no_suitable_candidates", "candidates": [], "excluded": excl,
                        "jev_used": True}

    # flat を先、metered を後に置く（S-8.4 の 3）
    flat = [m for m in pool if m.get("billing", "flat") == "flat"]
    metered = [m for m in pool if m.get("billing", "flat") != "flat"]
    # metered で単価未登録の候補は最安と確定せず、候補から外して理由を記録する
    unpriced: list[dict] = []
    priced: list[dict] = []
    for m in metered:
        if _price(m) is None:
            unpriced.append({"id": m["id"], "reason": "metered_unpriced"})
        else:
            priced.append(m)
    excl.extend(unpriced)
    if flat:
        chosen_pool = flat
    elif priced:
        chosen_pool = priced
    else:
        return {"selected": None, "reason": "parked_metered_unpriced", "candidates": [], "excluded": excl,
                "jev_used": bool(jev_available)}

    def key_fn(m: dict):
        return (_tier_rank(m.get("tier", "standard")), _role_order(role, m, knobs), m["id"])

    chosen_pool_sorted = sorted(chosen_pool, key=key_fn)
    # 枠の検査（S-8.7。満枠は枠を待つ理由になる。ここでは候補を残して理由に書く）
    picked = chosen_pool_sorted[0]
    return {
        "selected": picked["id"],
        "reason": "cheapest_eligible",
        "candidates": [m["id"] for m in chosen_pool_sorted],
        "excluded": excl,
        "jev_used": bool(knobs.jev_enabled and jev_available and jev_scores),
        "role_order": _role_order(role, picked, knobs),
        "family_cap": dict(_fc or {}),
        "model_cap": dict(_mc or {}),
    }


def slots_for(models: list[dict], jobs: list[dict], knobs: Knobs | None = None) -> dict:
    """枠の数え方（D-7.4）。jobs の launching・submitted・running を数え出す。別の表は持たない。"""
    knobs = knobs or Knobs()
    active = [j for j in jobs if j.get("state") in ("launching", "submitted", "running")]
    by_model: dict[str, int] = {}
    for j in active:
        if j.get("model"):
            by_model[j["model"]] = by_model.get(j["model"], 0) + 1
    by_family: dict[str, int] = {}
    fam_of = {m["id"]: m.get("family", "") for m in models}
    for j in active:
        if j.get("model"):
            fam = fam_of.get(j["model"], "")
            by_family[fam] = by_family.get(fam, 0) + 1
    # free の実効上限は min（系統の調整値、active な無料モデルの個別上限の合計）（S-8.7、D-7.4）
    free_caps = [m.get("cap") or 0 for m in models if m.get("tier") == "free" and m.get("status") == "active"]
    family_caps = dict(knobs.family_cap or {})
    if "free" in family_caps or free_caps:
        family_caps["free"] = min(family_caps.get("free", sum(free_caps) or 10**9), sum(free_caps)) if free_caps else family_caps.get("free", 10**9)
    model_caps = dict(knobs.model_cap or {})
    for m in models:
        if isinstance(m.get("cap"), int) and m["cap"] >= 0 and m["id"] not in model_caps:
            model_caps[m["id"]] = m["cap"]
    return {"by_model": by_model, "by_family": by_family, "family_cap": family_caps, "model_cap": model_caps}


def free_cap_total(models: list[dict]) -> int:
    return sum((m.get("cap") or 0) for m in models if m.get("tier") == "free" and m.get("status") == "active")
