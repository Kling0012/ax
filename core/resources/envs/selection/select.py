"""環境の選び方（S-25.4、D-23）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from axcommon.policy import allowed

from ..contracts import SelectionRequest

_COST_ORDER = {"free": 0, "fixed": 1, "metered": 2}


@dataclass(frozen=True)
class SelectionResult:
    """選択器の観測結果。枠は取らず、環境 ID だけを返す。"""

    selected: str | None
    candidates: tuple[dict[str, Any], ...]
    excluded: tuple[dict[str, str], ...]
    effective_sensitivity: str


def _cost_kind(environment: Mapping[str, Any]) -> str:
    cost = environment.get("cost", {})
    return cost.get("kind", "metered") if isinstance(cost, Mapping) else str(cost)


def _slot_free(environment: Mapping[str, Any]) -> bool:
    if "slot_free" in environment:
        return bool(environment["slot_free"])
    return int(environment.get("running", 0)) < int(environment.get("max_concurrent", 0))


def _checked_capabilities(environment: Mapping[str, Any], requires: tuple[str, ...]) -> bool:
    if not set(requires) <= set(environment.get("capabilities", ())):
        return False
    if environment.get("adapter") != "cloud-sandbox":
        return True
    checked = (environment.get("probe") or {}).get("capabilities_checked", {})
    return all(checked.get(capability) == "ok" for capability in requires)


def _allowed_by_effective_classes(environment: Mapping[str, Any], sensitivity: str) -> bool:
    declared = set(environment.get("data_classes") or ())
    effective = set(environment.get("effective_data_classes", declared))
    narrowed = declared & effective
    target = dict(environment)
    target["data_classes"] = list(narrowed)
    return allowed(sensitivity, target)


def _reason(environment: Mapping[str, Any], request: SelectionRequest) -> str | None:
    environment_id = environment.get("id")
    state = environment.get("state")
    if state == "trial":
        if request.trial_for != environment_id:
            return "trial_excluded"
        if request.effective_sensitivity != "public":
            return "sensitivity_denied"
    elif state != "available":
        return "state_not_available"
    if not _checked_capabilities(environment, request.requires):
        return "capability_unverified"
    if not _allowed_by_effective_classes(environment, request.effective_sensitivity):
        return "sensitivity_denied"
    if environment.get("owner_use") and not request.user_requested:
        return "sensitivity_denied"
    if environment.get("lifetime") == "rental" and request.resident_output:
        return "resident_output_denied"
    if not _slot_free(environment):
        return "slot_busy"
    return None


def _order(environment: Mapping[str, Any], index: int) -> tuple[Any, ...]:
    return (
        _COST_ORDER.get(_cost_kind(environment), len(_COST_ORDER)),
        int(environment.get("rank", 100)),
        float(environment.get("day_use_s", 0)),
        str(environment.get("registered_at", "")),
        index,
    )


def select(environments: Iterable[Mapping[str, Any]], request: SelectionRequest) -> SelectionResult:
    """能力、区分、空き、費用、rank、日利用量、登録順で候補を並べる。

    `scope: each` でも候補を全件返すだけで、枠の確保は呼び出し側の取引で行う。
    """
    ordered: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    excluded: list[dict[str, str]] = []
    for index, environment in enumerate(environments):
        reason = _reason(environment, request)
        environment_id = str(environment.get("id", f"#{index}"))
        if reason:
            excluded.append({"env_id": environment_id, "reason": reason})
            continue
        ordered.append((_order(environment, index), {
            "env_id": environment_id,
            "day_use_s": environment.get("day_use_s", 0),
            "slot_free": True,
        }))
    candidates = tuple({**candidate, "order": order} for order, (_, candidate) in enumerate(sorted(ordered), 1))
    return SelectionResult(
        selected=candidates[0]["env_id"] if candidates else None,
        candidates=candidates,
        excluded=tuple(excluded),
        effective_sensitivity=request.effective_sensitivity,
    )
