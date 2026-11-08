"""docker compose の出力の読み取り（ps の JSON、config の JSON）。コマンドの組み立ては config.HostConfig.compose。"""
from __future__ import annotations

import json

BAD_STATES = frozenset({"exited", "dead", "created"})  # 起動し直す状態（restarting は Docker が再起動中なので触らない）


def parse_ps(out: str) -> dict[str, dict]:
    """`compose ps --all --format json` の出力 -> {サービス名: {"state", "health", "name"}}。

    Compose の版により、1 行 1 オブジェクト（NDJSON）か、配列 1 つ。どちらも読む。同じサービスが複数あれば悪い方を残す。
    """
    text = out.strip()
    if not text:
        return {}
    try:
        whole = json.loads(text)
    except ValueError:
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        items = [whole] if isinstance(whole, dict) else whole
    if not isinstance(items, list) or not all(isinstance(it, dict) for it in items):
        raise ValueError("invalid compose ps output")
    res: dict[str, dict] = {}
    for it in items:
        svc = it.get("Service")
        if not isinstance(svc, str) or not isinstance(it.get("State"), str):
            raise ValueError("incomplete compose ps output")
        row = {"state": str(it.get("State", "")).lower(), "health": str(it.get("Health", "") or "").lower(), "name": str(it.get("Name", ""))}
        if svc in res and not needs_up(row) and needs_up(res[svc]):
            continue
        res[svc] = row
    return res


def needs_up(row: dict | None) -> bool:
    """状態が悪い（止まっている・無い・unhealthy）か。"""
    if row is None:
        return True
    return row["state"] in BAD_STATES or row["health"] == "unhealthy"


def parse_services(out: str) -> list[str]:
    """`compose config --services` の出力。"""
    return [l.strip() for l in out.splitlines() if l.strip()]


def parse_config_json(out: str) -> dict[str, dict]:
    """`compose config --format json` -> {サービス名: 定義}。"""
    obj = json.loads(out)
    svcs = obj.get("services") if isinstance(obj, dict) else None
    if not isinstance(svcs, dict) or not svcs or not all(isinstance(v, dict) for v in svcs.values()):
        raise ValueError("invalid compose config output")
    return svcs
