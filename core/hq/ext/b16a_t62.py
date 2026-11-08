"""Read-only HQ views for the service index, map and Hibi state."""
from __future__ import annotations

import html
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse
from starlette.routing import Route

from core.hq.api import authorize
from axcommon.client import TimelineClient, service_url


def esc(value):
    return html.escape(str(value), quote=True)


class Snapshot:
    """Retain a last successful read while exposing the most recent error."""
    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.last_success_at = None

    def read(self, loader):
        with self.lock:
            try:
                data = loader()
                self.data = data
                self.last_success_at = datetime.now(timezone.utc).isoformat()
                error = None
            except Exception:
                error = "fetch_failed"
            return {"data": self.data, "last_success_at": self.last_success_at, "error": error}


def read_json(path):
    if not path:
        raise ValueError("source_not_registered")
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def sites_source():
    data = read_json(os.environ.get("AX_ENTRY_SITES_FILE"))
    if not isinstance(data, dict) or not isinstance(data.get("sites"), list):
        raise ValueError("invalid_sites")
    if any(not isinstance(row, dict) for row in data["sites"]):
        raise ValueError("invalid_site")
    return data


def map_source():
    data = read_json(os.environ.get("AX_AUTOMATION_MAP_SOURCE"))
    if not isinstance(data, (dict, list)):
        raise ValueError("invalid_map")
    return data


def page(title, body, *, refresh):
    return HTMLResponse('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
                        f'<meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="{refresh}"><title>{esc(title)}</title>'
                        '</head><body><nav><a href="/entry">入口</a> '
                        '<a href="/hq/map">地図</a> <a href="/hq/hibi">Hibi</a></nav>'
                        f'<main><h1>{esc(title)}</h1>{body}</main></body></html>',
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'", "X-Content-Type-Options": "nosniff"})


def status(snapshot):
    current = "取得に失敗しました" if snapshot["error"] else "取得しました"
    last = snapshot["last_success_at"] or "なし"
    return f'<p role="status">{current}。最終成功時刻: {esc(last)}</p>'


def age_notice(data, interval_s):
    if not isinstance(data, dict):
        return ""
    try:
        stamp = datetime.fromisoformat(data["as_of"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("missing timezone")
        if (datetime.now(timezone.utc) - stamp).total_seconds() > interval_s * 3:
            return '<p role="status">最後の値は古くなっています</p>'
    except (AttributeError, KeyError, TypeError, ValueError):
        return '<p role="status">最後の値の時刻は不明です</p>'
    return ""


def render_sites(data):
    if data is None:
        return ""
    rows = []
    for row in data["sites"]:
        name = esc(row.get("name") or row.get("id") or "")
        url = row.get("url")
        if isinstance(url, str):
            try:
                parsed = urlsplit(url)
                if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
                    name = f'<a href="{esc(url)}" rel="noreferrer">{name}</a>'
            except ValueError:
                pass
        mark = ' <strong>公開中</strong>' if row.get("exposure") == "public" else ""
        rows.append(f'<li>{name}{mark}</li>')
    return f'<p>候補: {len(rows)} 件</p><ul>' + "".join(rows) + '</ul>'


def render_data(data):
    if data is None:
        return ""
    return '<pre>' + esc(json.dumps(data, ensure_ascii=False, indent=2)) + '</pre>'


def render_map(data):
    if not isinstance(data, dict) or not isinstance(data.get("maps"), list):
        return render_data(data)
    sections = []
    for item in data["maps"]:
        if not isinstance(item, dict):
            continue
        boxes = item.get("boxes") if isinstance(item.get("boxes"), list) else []
        flows = item.get("flows") if isinstance(item.get("flows"), list) else []
        elements = "".join(f"<li>{esc(box.get('name', ''))}</li>" for box in boxes if isinstance(box, dict))
        connections = "".join(f"<li>{esc(flow.get('name', ''))}: {esc(' → '.join(map(str, flow.get('steps', []))))}</li>"
                              for flow in flows if isinstance(flow, dict) and isinstance(flow.get("steps", []), list))
        sections.append(f"<section><h2>{esc(item.get('name', ''))}</h2><p>{esc(item.get('one', ''))}</p>"
                        f"<ul>{elements}</ul><ul>{connections}</ul></section>")
    return "".join(sections)


def register(app):
    sites, maps, focus = (Snapshot() for _ in range(3))
    daily_by_date = {}
    observations = Snapshot()
    timeline = TimelineClient(service_url("timeline"))

    def hibi_observation():
        client = getattr(app.state, "hibi_observation_client", timeline)
        after = 0
        latest = None
        for _ in range(100):
            result = client.get_events(after=after, kind="automation.observed", subloop="hibi-state", subject="focus", limit=1000)
            events = result.get("events")
            if not isinstance(events, list):
                raise ValueError("invalid_observations")
            for event in events:
                data = event.get("data", {})
                if data.get("plugin_id") != "hibi-state" or data.get("target_id") != "focus":
                    continue
                stamp = datetime.fromisoformat(data["as_of"].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError("invalid_observation_time")
                if latest is None or stamp >= latest[0]:
                    latest = (stamp, data.get("result"))
            if len(events) < 1000:
                break
            next_after = result.get("next_after")
            if type(next_after) is not int or next_after <= after:
                raise ValueError("observation_pagination_stalled")
            after = next_after
        else:
            raise ValueError("observation_limit")
        if latest is None:
            return "更新状況は不明です"
        result = latest[1]
        if isinstance(result, str) and result.startswith("failed:"):
            return "状態の更新に失敗しました"
        if result in ("skipped_unknown", "skipped_mode", "skipped_busy"):
            return "状態を更新できませんでした"
        if result in ("stored", "unchanged"):
            return "状態を更新しました"
        return "更新状況は不明です"

    def interval(name):
        try:
            value = app.state.hq_api.defaults.get(name)
            return value if type(value) is int and value > 0 else 60
        except Exception:
            return 60

    async def entry(request):
        denied = await authorize(request, local=True)
        if denied:
            return denied
        state_path = os.environ.get("AX_ENTRY_STATE_FILE")
        def load_state():
            state = read_json(state_path)
            data, stamp, latest = state["data"], state["last_success_at"], state["latest"]
            if data is not None and (not isinstance(data, dict) or not isinstance(data.get("sites"), list)
                                     or any(not isinstance(row, dict) for row in data["sites"])):
                raise ValueError("invalid_state")
            if not isinstance(latest, dict) or latest.get("status") not in ("success", "failed"):
                raise ValueError("invalid_state")
            if not isinstance(latest.get("at"), str) or datetime.fromisoformat(latest["at"].replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("invalid_state")
            if data is not None and (not isinstance(stamp, str) or datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is None):
                raise ValueError("invalid_state")
            if latest["status"] == "success" and (data is None or stamp != latest["at"]):
                raise ValueError("invalid_state")
            return {"data": data, "last_success_at": stamp, "error": latest["status"] != "success"}
        cached = await run_in_threadpool(sites.read, load_state)
        snapshot = cached["data"] or {"data": None, "last_success_at": None, "error": True}
        if cached["error"]:
            snapshot = {**snapshot, "error": True}
        return page("サービス入口", status(snapshot) + '<p>Cloudflare などの外部入口は収集対象外です</p>' + render_sites(snapshot["data"]), refresh=interval("u_entry_interval_s"))

    async def map_view(request):
        denied = await authorize(request)
        if denied:
            return denied
        snapshot = await run_in_threadpool(maps.read, map_source)
        return page("仕組みの地図", status(snapshot) + render_map(snapshot["data"]), refresh=interval("u_entry_interval_s"))

    async def hibi_view(request):
        denied = await authorize(request)
        if denied:
            return denied
        api = request.app.state.hq_api
        today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).date().isoformat()

        def load(path):
            code, result = api.read(path)
            if code != 200 or result.get("ok") is not True:
                raise ValueError("fetch_failed")
            return result["data"]

        now = await run_in_threadpool(focus.read, lambda: load("/v1/focus-state"))
        day = await run_in_threadpool(daily_by_date.setdefault(today, Snapshot()).read, lambda: load(f"/v1/daily-state/{today}"))
        observed = await run_in_threadpool(observations.read, hibi_observation)
        update_status = "更新状況を取得できません" if observed["error"] else observed["data"]
        return page("Hibi", '<p role="status">' + esc(update_status) + "</p><h2>集中状態</h2>" + status(now) + age_notice(now["data"], interval("u_hibi_interval_s")) + render_data(now["data"])
                    + "<h2>一日の状態</h2>" + status(day) + age_notice(day["data"], interval("u_hibi_interval_s")) + render_data(day["data"]), refresh=interval("u_hibi_interval_s"))

    app.router.routes.extend([Route("/entry", entry, methods=["GET"]),
                              Route("/hq/map", map_view, methods=["GET"]),
                              Route("/hq/hibi", hibi_view, methods=["GET"])])
