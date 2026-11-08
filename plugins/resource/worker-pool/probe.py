"""prefix_cache の計測（S-8.12 の 14、T-113、D-7.5）。

worker-pool が自分で出す。管理役は出さない。prefix_cache が unknown のモデルごとに、
同じ prefix_id の軽い作業（読み取りだけの summary 相当の固定の依頼）を
prefix_probe_gap_s 秒以内の間隔で 2 回、並び A で走らせ、2 回目の初回の cache_read の
比率を記録する。モデルは、worker-pool の内部の関数 submit_internal だけが持てる項目
pin_model で固定する（S-8.4 の選択を通さない）。POST /v1/jobs と MCP の本文に
pin_model があれば 400（pin_model_not_allowed、S-8.8）で拒否する。

- prefix_hit_floor 以上なら on、未満なら off、比率が取れなければ unknown のままにする。
- 結果は resources の POST /v1/resources/{id}/prefix_cache（value、ratio、measured_at）で書く。
  呼べるのは worker-pool の資格だけ（D-7.3）。
- 計測の契機は、unknown のモデルが登録簿にあることだけである。B6 で初めて動かすと全モデルが対象になる。
- 計測のジョブは通常の枠と系統の上限に従い、空きが無いときは待つ。
- 構築者は B6 の受け入れで、worker-pool の内部コマンド ax-wp-probe-prefix
  （コンテナ内で docker compose exec により呼ぶ）で即時に走らせてよい。
"""
from __future__ import annotations

from typing import Any

from axcommon import timeutil
from axcommon.defaults import Defaults

from .prompt import PromptParts, prefix_id

PROBE_PROMPT = "summary probe: report the current standing-rules title only."
PROBE_ROLE = "summary"


def _default(name: str, fallback: Any) -> Any:
    try:
        return Defaults.load().get(name)
    except Exception:  # noqa: BLE001
        return fallback


def probe_parts(*, standing: str = "standing-rules", project: str = "", lessons: tuple = ()) -> PromptParts:
    """計測の固定部分。同じ prefix_id を 2 回に共通させる。"""
    return PromptParts(standing=standing, project=project, lessons=lessons, job=PROBE_PROMPT)


def probe_prefix_id(**kw) -> str:
    parts = probe_parts(**kw)
    return prefix_id(parts)


def within_gap(first_at: str | None, second_at: str | None, *, gap_s: int | None = None) -> bool:
    """prefix_probe_gap_s 秒以内に 2 回目が始まったか。"""
    if first_at is None or second_at is None:
        return False
    gs = gap_s if gap_s is not None else (_default("prefix_probe_gap_s", 120) or 120)
    try:
        a = timeutil.parse_ts(first_at)
        b = timeutil.parse_ts(second_at)
    except ValueError:
        return False
    return 0 <= (b - a).total_seconds() <= gs


def decide(*, ratio: float | None, floor: float | None = None) -> str:
    """比率から prefix_cache の値を決める（unknown のままを許す）。"""
    fl = floor if floor is not None else (_default("prefix_hit_floor", 0.5) or 0.5)
    if ratio is None:
        return "unknown"
    return "on" if ratio >= fl else "off"


def is_unknown_models(models: list[dict]) -> list[str]:
    """prefix_cache が unknown のモデルの一覧（B6 では全モデルが対象）。"""
    return [m["id"] for m in models if m.get("kind") == "model" and m.get("prefix_cache", "unknown") == "unknown"]


def post_body(*, value: str, ratio: float | None = None, measured_at: str | None = None) -> dict:
    """resources への POST /v1/resources/{id}/prefix_cache の本文（D-7.3）。"""
    return {"value": value, "ratio": ratio, "measured_at": measured_at or timeutil.now_ts()}
