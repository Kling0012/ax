"""使用量の記録（S-8.12 の 12、D-7.5、T-111、R-N49）。

各試行の終了時に、使用量（input、cache_read、cache_creation、output）と、初回ターンの
first_input、first_cache_read、最後のターンの文脈 last_context を記録する。
提供元が報告しない項目は「不明」とし、0 とは区別する。取得に失敗しても、ジョブの
成否と他の機能に影響させない。

runner の JSON（usage、modelUsage、session_id）は、launcher が保存したコンテナのログ
（.container-<job_id>.log）の最後の JSON から読む。first_input / first_cache_read と
last_context は launcher が .usage-<job_id>.json に書いた物を worker-pool が読む（F-18.2）。
集計は worker.usage の事象から作る表示で、別の表を持たない。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def parse_container_log(log_text: str) -> dict | None:
    """コンテナのログの最後の JSON を読む。無ければ None（「不明」と記録する）。"""
    if not log_text:
        return None
    last = None
    for line in log_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and ("usage" in doc or "result" in doc):
            last = doc
    return last


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, int) and not isinstance(v, bool) else None


def usage_of(container_doc: dict | None, launcher_usage: dict | None = None) -> dict:
    """使用量の記録の形。報告されない項目は None（不明）。"""
    u = (container_doc or {}).get("usage") or {}
    lu = launcher_usage or {}
    cache_read = _int(u.get("cache_read_input_tokens"))
    cache_creation = _int(u.get("cache_creation_input_tokens"))
    inp = _int(u.get("input_tokens"))
    out = _int(u.get("output_tokens"))
    first_input = lu.get("first_input") if isinstance(lu.get("first_input"), int) else None
    first_cache_read = lu.get("first_cache_read") if isinstance(lu.get("first_cache_read"), int) else None
    last_context = lu.get("last_context") if isinstance(lu.get("last_context"), int) else None
    ratio = None
    if first_input is not None and first_cache_read is not None and first_input > 0:
        ratio = first_cache_read / first_input
    return {
        "input": inp,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "output": out,
        "first_input": first_input,
        "first_cache_read": first_cache_read,
        "first_cache_ratio": ratio,
        "last_context": last_context,
        "unknown": [k for k, v in (("input", inp), ("cache_read", cache_read), ("cache_creation", cache_creation),
                                    ("output", out)) if v is None],
    }


def read_launcher_usage(path: str | Path) -> dict | None:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def session_id_from(container_doc: dict | None) -> str | None:
    if not isinstance(container_doc, dict):
        return None
    sid = container_doc.get("session_id")
    return sid if isinstance(sid, str) else None


def event_data(*, job_id: str, attempt_id: str, usage: dict, session_id: str | None = None,
               session_id_updated: bool = False) -> dict:
    """worker.usage 事象の data。秘密や依頼本文は入れない。"""
    return {"job_id": job_id, "attempt_id": attempt_id, "usage": usage,
            "session_id": session_id, "session_id_updated": session_id_updated}


def aggregate_by_role_model(events: list[dict]) -> list[dict]:
    """役割・モデル・runner ごとの初回読み込み比率（S-8.12 の 13）。prefix_hit_floor を割った
    組み合わせは自動で止めず、管理役が点検で提案する。"""
    groups: dict[tuple, list[dict]] = {}
    for ev in events:
        data = ev.get("data") or {}
        key = (data.get("role"), data.get("model"), data.get("runner"))
        groups.setdefault(key, []).append(data.get("usage") or {})
    out: list[dict] = []
    for (role, model, runner), usages in groups.items():
        ratios = [u["first_cache_ratio"] for u in usages if isinstance(u.get("first_cache_ratio"), (int, float))]
        if not ratios:
            continue
        ratios_sorted = sorted(ratios)
        median = ratios_sorted[len(ratios_sorted) // 2] if len(ratios_sorted) % 2 else (ratios_sorted[len(ratios_sorted)//2 - 1] + ratios_sorted[len(ratios_sorted)//2]) / 2
        out.append({"role": role, "model": model, "runner": runner, "n": len(ratios), "median_ratio": median})
    return out
