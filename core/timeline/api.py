"""時系列記録の API（S-3.5）。中核の口 8080 と作業側の口 8081 の 2 つのアプリを、同じ Service から作る。

8080（ax-core。全 API）:
  POST /v1/events                  1 件か配列で追記。id が既にあれば無視して成功
  GET  /v1/events                  after、kind（カンマ区切り可）、subloop、project、subject、limit、wait
  GET  /v1/events/{id}
  GET  /v1/projections/{name}      plugins / runs / jobs / controls / health。ほかの名前は 404
  POST /v1/export                  範囲を指定して JSON Lines に書き出す（退避コネクタが archive/ から移す）
8081（ax-work。許可した API だけ。F-18.4）:
  POST /v1/events、GET /v1/events、GET /v1/events/{id} だけ。ほかは 404。
  資格: Authorization: Bearer <プラグインのトークン>。ハッシュの一覧で呼び出し元のプラグイン ID を決める。一覧に無ければ 401。
  source.component はプラグイン ID と同じでなければ拒否（403 component_mismatch）。kind は kinds.WORK_SIDE_KINDS か manifest が宣言した物だけ
  （違えば 403 kind_not_allowed）。拒否したときは events.rejected を書く。principal・session・via は本文から受けない（403 reserved_field）。
更新・削除の経路（PUT / PATCH / DELETE）は持たない（T-1 がルーティング表を検査する）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon import creds, ids, schemas, timeutil
from axcommon.events import build_event
from axcommon.http import json_error, read_json

from . import COMPONENT, VERSION, kinds, projectors
from .archive import ArchiveError, TimelineArchive
from .store import RebuildError, Store

log = logging.getLogger("timeline")

MAX_BATCH = 1000
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _structural_event_validator():
    """事象の形（S-3.2）だけを検査する検査器。event.schema.json の kind ごとの data の条件（allOf）は外す。
    S-3.6 と T-117: 投影の必須の項目が無い事象も、追記は成功させ、投影だけ飛ばす（fail-soft）。data の中身で記録を断らない。"""
    try:
        import jsonschema
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
    except ImportError:  # 古い jsonschema は kind ごとの条件つきの検査器で代用する
        return schemas.validator("event")
    docs = dict(schemas._all_docs())
    ev = dict(schemas.load("event"))
    ev.pop("allOf", None)
    docs[ev["$id"]] = ev
    reg = Registry().with_resources((u, Resource.from_contents(d, default_specification=DRAFT202012)) for u, d in docs.items())
    return jsonschema.Draft202012Validator(ev, registry=reg, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)


class StoreSink:
    """timeline 自身の事象（health.beat、events.rejected、health.alert）を、同じプロセスの Store に直接書く。
    axcommon.http.create_app の sink として使える（emit / flush）。例外を出さない。"""

    def __init__(self, service: "Service"):
        self.service = service
        self.outbox = []

    def emit(self, kind: str, subject: str, data: dict | None = None, **kw) -> dict | None:
        try:
            ev = build_event(component=COMPONENT, host=self.service.host, kind=kind, subject=subject, data=data, **kw)
            self.service.append_sync([ev])
            return ev
        except Exception:  # noqa: BLE001  自分の事象が書けなくても部品を止めない
            log.exception("自分の事象を書けない: %s", kind)
            return None

    def flush(self) -> int:
        return 0


class Service:
    def __init__(self, store: Store, *, host: str, redactor=None, archive: TimelineArchive | None = None, hot_days: int = 30,
                 longpoll_s: float = 30, tokens: creds.TokenTable | None = None,
                 manifest_kinds: kinds.ManifestKinds = kinds.no_manifest_kinds):
        self.store, self.host, self.redactor, self.archive = store, host, redactor, archive
        self.hot_days, self.longpoll_s = hot_days, longpoll_s
        self.tokens = tokens
        self.manifest_kinds = manifest_kinds
        self.sink = StoreSink(self)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cond: asyncio.Condition | None = None
        self._event_validator = _structural_event_validator()
        self.rejected_total = 0
        self._index_alerted = False

    # --- 追記 -----------------------------------------------------------------------------------
    def _redact(self, data: Any) -> Any:
        if self.redactor is None:
            return data
        return self.redactor.redact_obj(data)[0]

    def append_sync(self, events: list[dict]) -> list[dict]:
        res = self.store.append(events, redactor=self._redact)
        if any(not r["duplicate"] for r in res):
            self._notify_threadsafe()
        self._check_index_health()
        return res

    def _check_index_health(self) -> None:
        """索引が読めないときは他の基準だけで動き、health.alert を 1 回書く（S-15.3）。戻ったら次の失敗でまた書く。"""
        rd = self.redactor
        if rd is None or getattr(rd, "index_path", None) is None:
            return
        ok = rd.reload_index_if_changed()
        if not ok and not self._index_alerted:
            self._index_alerted = True
            ev = build_event(component=COMPONENT, host=self.host, kind="health.alert", subject="redact-index",
                             data={"check": "redact_index", "target": COMPONENT, "severity": "warning"})
            self.store.append([ev], redactor=self._redact)
        elif ok:
            self._index_alerted = False

    def _notify_threadsafe(self) -> None:
        loop, cond = self._loop, self._cond
        if loop is None or cond is None or loop.is_closed():
            return

        async def _n() -> None:
            async with cond:
                cond.notify_all()

        try:
            asyncio.run_coroutine_threadsafe(_n(), loop)
        except RuntimeError:
            pass

    def bind_loop(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._cond = asyncio.Condition()

    # --- 検査 -----------------------------------------------------------------------------------
    def validate_event(self, ev: Any) -> list[str]:
        """事象の形（S-3.2）の検査。kind ごとの data の条件は見ない（上の _structural_event_validator）。"""
        return sorted(schemas._fmt(e) for e in self._event_validator.iter_errors(ev))


def _int_param(request: Request, name: str, default: int, lo: int, hi: int | None = None) -> tuple[int | None, JSONResponse | None]:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default, None
    try:
        v = int(raw)
    except ValueError:
        return None, json_error(422, "bad_request", f"{name} は整数")
    if v < lo or (hi is not None and v > hi):
        return None, json_error(422, "bad_request", f"{name} は {lo} 以上" + (f" {hi} 以下" if hi is not None else ""))
    return v, None


def _bool_param(request: Request, name: str) -> bool | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return False
    if raw.lower() in ("true", "1"):
        return True
    if raw.lower() in ("false", "0"):
        return False
    return None


def build_routes(svc: Service, *, work_side: bool) -> list[Route]:
    """work_side=True は 8081 用。認証と制限を付け、読み取りと追記以外の経路を持たない。"""

    def caller(request: Request) -> tuple[str | None, JSONResponse | None]:
        if not work_side:
            return None, None
        t = svc.tokens
        if t is None:
            # プラグインのトークンのハッシュの一覧の置き場が文書で決まっていない（OPEN.md）。設定が無い間は全て 401（閉じる側に倒す）
            return None, json_error(401, "unauthorized", "資格の一覧が設定されていない")
        t.reload_if_changed()
        pid = t.identify(creds.parse_bearer(request.headers.get("authorization")))
        if pid is None:
            return None, json_error(401, "unauthorized", "資格が一覧にない")
        return pid, None

    async def post_events(request: Request) -> JSONResponse:
        pid, err = caller(request)
        if err:
            return err
        body, err = await read_json(request)
        if err:
            return err
        events = body if isinstance(body, list) else [body]
        if not events:
            return json_error(422, "bad_request", "事象が空")
        if len(events) > MAX_BATCH:
            return json_error(413, "too_large", f"1 回の追記は {MAX_BATCH} 件まで")
        if work_side:  # 権限の検査を、形の検査より先に行う（書いてはいけない kind の中身を調べて答えない）
            for ev in events:
                reason = _work_side_reject(svc, pid, ev)
                if reason:
                    await asyncio.to_thread(_write_rejected, svc, pid, ev, reason)
                    return json_error(403, reason, "この口では書けない事象")
        for i, ev in enumerate(events):
            errs = svc.validate_event(ev)
            if errs:
                return json_error(422, "bad_request", f"events[{i}]: " + "; ".join(errs[:3]))
        res = await asyncio.to_thread(svc.append_sync, events)
        await _notify(svc)
        return JSONResponse({"accepted": sum(1 for r in res if not r["duplicate"]), "duplicates": sum(1 for r in res if r["duplicate"]),
                             "events": res})

    async def get_events(request: Request) -> JSONResponse:
        _, err = caller(request)
        if err:
            return err
        q = request.query_params
        after, err = _int_param(request, "after", 0, 0)
        if err:
            return err
        limit, err = _int_param(request, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        if err:
            return err
        try:
            wait = float(q.get("wait", "0") or 0)
        except ValueError:
            return json_error(422, "bad_request", "wait は秒数")
        if wait < 0:
            return json_error(422, "bad_request", "wait は 0 以上")
        wait = min(wait, svc.longpoll_s)
        kind_list = [k for k in (q.get("kind") or "").split(",") if k] or None

        def fetch() -> list[dict]:
            return svc.store.list_events(after=after, kinds=kind_list, subloop=q.get("subloop"), project=q.get("project"),
                                         subject=q.get("subject"), limit=limit)

        events = await asyncio.to_thread(fetch)
        if not events and wait > 0 and svc._cond is not None:
            deadline = time.monotonic() + wait
            cond = svc._cond
            while not events:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                async with cond:
                    # 鍵を持ったまま読み直してから待つ（追記の通知は commit の後、同じ鍵を取って送るので取りこぼさない）
                    events = await asyncio.to_thread(fetch)
                    if events:
                        break
                    try:
                        await asyncio.wait_for(cond.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                events = await asyncio.to_thread(fetch)
        next_after = events[-1]["seq"] if events else after
        return JSONResponse({"events": events, "next_after": next_after})

    async def get_event(request: Request) -> JSONResponse:
        _, err = caller(request)
        if err:
            return err
        ev = await asyncio.to_thread(svc.store.get_event, request.path_params["event_id"])
        if ev is None:
            return json_error(404, "not_found", "その ID の事象がない")
        return JSONResponse(ev)

    async def get_projection(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if name not in projectors.PROJECTION_NAMES:
            return json_error(404, "not_found", "その名前の表示用データはない")
        q = request.query_params
        active = _bool_param(request, "active")
        if active is None:
            return json_error(422, "bad_request", "active は true / false")
        if active and name not in projectors.ACTIVE_NAMES:
            return json_error(422, "bad_request", "active=true は runs、jobs、controls だけ")
        limit, err = _int_param(request, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        if err:
            return err
        out = await asyncio.to_thread(lambda: svc.store.projection(name, key=q.get("key"), active=active, limit=limit))
        return JSONResponse(out)

    async def post_export(request: Request) -> JSONResponse:
        if svc.archive is None:
            return json_error(503, "unavailable", "書き出し先が設定されていない")
        raw = await request.body()
        body: Any = {}
        if raw.strip():
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError:
                return json_error(400, "bad_json", "本文が JSON でない")
        if not isinstance(body, dict) or set(body) - {"before_ts", "to_seq", "days"}:
            return json_error(422, "bad_request", "before_ts、to_seq、days のどれかだけを渡す")
        before_ts, to_seq = body.get("before_ts"), body.get("to_seq")
        if "days" in body:
            days = body["days"]
            if not isinstance(days, (int, float)) or isinstance(days, bool) or days < 0:
                return json_error(422, "bad_request", "days は 0 以上の数")
            before_ts = timeutil.add_seconds(timeutil.now_ts(), -86400 * days)
        if before_ts is None and to_seq is None:
            before_ts = timeutil.add_seconds(timeutil.now_ts(), -86400 * svc.hot_days)
        if before_ts is not None:
            try:
                timeutil.parse_ts(before_ts)
            except (ValueError, TypeError):
                return json_error(422, "bad_request", "before_ts は RFC 3339")
        if to_seq is not None and (not isinstance(to_seq, int) or isinstance(to_seq, bool) or to_seq < 1):
            return json_error(422, "bad_request", "to_seq は 1 以上の整数")
        try:
            res = await asyncio.to_thread(lambda: svc.archive.export(svc.store, before_ts=before_ts, to_seq=to_seq))
        except (ArchiveError, OSError) as e:
            log.error("書き出しに失敗: %s", type(e).__name__)
            return json_error(500, "export_failed", "書き出しに失敗した")
        return JSONResponse(res)

    if work_side:
        return [
            Route("/v1/events", post_events, methods=["POST"]),
            Route("/v1/events", get_events, methods=["GET"]),
            Route("/v1/events/{event_id}", get_event, methods=["GET"]),
        ]
    return [
        Route("/v1/events", post_events, methods=["POST"]),
        Route("/v1/events", get_events, methods=["GET"]),
        Route("/v1/events/{event_id}", get_event, methods=["GET"]),
        Route("/v1/projections/{name}", get_projection, methods=["GET"]),
        Route("/v1/export", post_export, methods=["POST"]),
    ]


async def _notify(svc: Service) -> None:
    if svc._cond is not None:
        async with svc._cond:
            svc._cond.notify_all()


def _work_side_reject(svc: Service, plugin_id: str | None, ev: Any) -> str | None:
    """形がまだ検査されていない本文に対して呼ばれるので、型を確かめながら読む。形の誤りは、ここでは拒否せず後の検査に任せる。"""
    if not isinstance(ev, dict) or not isinstance(ev.get("source"), dict):
        return None
    src = ev["source"]
    if src.get("component") != plugin_id:
        return "component_mismatch"
    if any(f in src for f in kinds.RESERVED_SOURCE_FIELDS):
        return "reserved_field"
    kind = ev.get("kind")
    if isinstance(kind, str) and not kinds.kind_allowed(plugin_id, kind, svc.manifest_kinds):
        return "kind_not_allowed"
    return None


def _write_rejected(svc: Service, plugin_id: str | None, ev: dict, reason: str) -> None:
    """events.rejected を書く。拒否された事象の本文は入れない（種類と理由だけ）。"""
    svc.rejected_total += 1
    try:
        rej = build_event(component=COMPONENT, host=svc.host, kind="events.rejected", subject=str(plugin_id or "unknown"),
                          data={"plugin": plugin_id, "reason": reason, "kind": str(ev.get("kind"))[:80], "event_id": str(ev.get("id"))[:40],
                                "claimed_component": str((ev.get("source") or {}).get("component"))[:80]})
        svc.append_sync([rej])
    except Exception:  # noqa: BLE001
        log.exception("events.rejected を書けない")
