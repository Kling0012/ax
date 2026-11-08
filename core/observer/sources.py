"""オブザーバーが読む物（時系列記録の投影と事象、部品の healthz、ホストの登録）。時系列記録が落ちているときは Unavailable を出す。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from axcommon import timeutil
from axcommon.client import ApiClient, ApiError, TimelineClient, Unavailable, service_url


def extract_events(resp: Any) -> list[dict]:
    """GET /v1/events の応答の形を 2 つ許す（配列か {events|items: […]}。S-3.5 に形が無い。OPEN.md の b0）。"""
    if isinstance(resp, list):
        return [e for e in resp if isinstance(e, dict)]
    if isinstance(resp, dict):
        for k in ("events", "items"):
            if isinstance(resp.get(k), list):
                return [e for e in resp[k] if isinstance(e, dict)]
    return []


def order_key(ev: dict) -> int:
    try:
        return int(ev.get("seq"))
    except (TypeError, ValueError):
        return 0


def parse_ts_or_none(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return timeutil.parse_ts(v)
    except ValueError:
        return None


class Sources:
    def __init__(self, timeline: TimelineClient, state_dir: str | os.PathLike, *,
                 probe: Callable[[str], bool] | None = None):
        self.timeline = timeline
        self.state_dir = Path(state_dir)
        self._probe = probe or self._default_probe

    # --- 時系列記録の投影 ---
    def projection(self, name: str, **params) -> tuple[dict[str, dict], int]:
        """{key: value} と upto_seq。"""
        resp = self.timeline.projection(name, **params)
        rows = (resp or {}).get("rows") or []
        out = {}
        for r in rows:
            if isinstance(r, dict) and isinstance(r.get("key"), str) and isinstance(r.get("value"), dict):
                out[r["key"]] = r["value"]
        upto = (resp or {}).get("upto_seq")
        return out, int(upto) if isinstance(upto, int) else 0

    def health(self) -> tuple[dict[str, dict], int]:
        return self.projection("health", limit=1000)

    def plugins(self) -> dict[str, dict]:
        return self.projection("plugins", limit=1000)[0]

    def controls(self) -> dict[str, dict]:
        return self.projection("controls", active="true")[0]

    def jobs(self) -> dict[str, dict]:
        return self.projection("jobs", limit=1000)[0]

    def events_after(self, kind: str, after: int, limit: int = 500) -> list[dict]:
        resp = self.timeline.get_events(after=after, kind=kind, limit=limit)
        evs = extract_events(resp)
        evs.sort(key=order_key)
        return evs

    # --- 部品の healthz（時系列記録が落ちているとき） ---
    @staticmethod
    def _default_probe(component: str) -> bool:
        try:
            body = ApiClient(service_url(component), timeout=3.0).healthz()
            return bool(isinstance(body, dict) and body.get("ok") is True)
        except (Unavailable, ApiError, ValueError):
            return False

    def probe(self, component: str) -> bool:
        try:
            return bool(self._probe(component))
        except Exception:  # noqa: BLE001  1 つの部品の確認の失敗で全体を止めない
            return False

    # --- ホスト見張りの登録（F-14.2） ---
    def registered(self) -> dict[str, float]:
        """登録済みのサービス名 -> 登録ファイルの更新時刻（epoch 秒）。ディレクトリが無ければ空。"""
        d = self.state_dir / "hostwatch" / "registered"
        out: dict[str, float] = {}
        try:
            for p in d.iterdir():
                if p.is_file() and not p.name.startswith("."):
                    try:
                        out[p.name] = p.stat().st_mtime
                    except OSError:
                        continue
        except OSError:
            pass
        return out


def component_of_service(service: str) -> str:
    """compose のサービス名 -> health.beat の component。サブループ sl-<id> はプラグイン ID（F-18.3）。"""
    return service[3:] if service.startswith("sl-") else service


def json_load(path: str | os.PathLike, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def json_save(path: str | os.PathLike, obj: Any) -> bool:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError:
        return False
