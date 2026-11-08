"""資源台帳の API（S-8.8、D-7.3）。

中核の口 8080（ax-core）。作業側の口 8081（ax-work。宣言した API だけ）:
  GET  /v1/resources/{id}            資源の 1 件
  GET  /v1/resources                 一覧（kind、status、family、tier の絞り込み）
  POST /v1/resources/{id}/outcome    worker-pool の成績の書き込み（D-7.3）
  POST /v1/resources/{id}/exclude    期限付きの一時除外（S-8.2）
  POST /v1/resources/{id}/include    一時除外の解除（S-8.2。対象だけ）
  GET  /v1/ledger                    モデルごとの実績（S-8.8）
  GET  /v1/models/discovery          提供一覧と登録簿の差（S-8.8）
  GET  /v1/selections                選択記録（S-8.4）
  GET  /v1/envs/select               path=G（S-25.27。core-tokens 資格だけ）
  POST /v1/envs/{id}/gate-token      worker-pool の資格だけ（S-25.27。NotImplemented の受け側）
  POST /v1/envs/{id}/gate-token/{token_id}/revoke   同上
  GET  /status と POST /apply        S-4.4

D-7.3: 資源の状態は resources が持つ。worker-pool は読むだけで、変更は outcome や exclude で頼む。
段階の上げ下げは resources が成績から計算する（S-8.5）。

core-tokens の受け側（F-18.4、S-25.27）: core-tokens.sha256 のハッシュ照合（core_auth.py）。
資格が無い API は 401、資格があっても範囲外は 403（credential_scope）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon import timeutil
from axcommon.http import json_error, read_json

from . import COMPONENT, VERSION
from .core_auth import CREDENTIAL_SCOPES, CoreAuth

log = logging.getLogger("resources")

ID_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,120}$")
REASONS = {"rate_limit", "quota", "connect", "capability", "manual", "periodic_check"}


class NotImplementedApi(Exception):
    """b14b1 が実装する分（S-25.27 の gate-token の発行そのもの）。

    資格と項目の検査はここで通す。本体の発行は core/resources/envs/ の実装が register する。
    未実装のうちは 501 で返し、その他を止めない。
    """


def _bearer(request: Request) -> str | None:
    return request.headers.get("authorization")


def _public(res: dict) -> dict:
    """応答に含める資源の形。秘密の値や依頼本文は含めない。"""
    out = dict(res)
    out.pop("stats", None)
    # stats は実績の参照に要るものだけ（対象期間、試行 ID、検証結果と出所）
    stats = res.get("stats") or {}
    trials = stats.get("trials") or []
    out["stats"] = {"trials": len(trials), "last": stats.get("last_outcome"), "prefix_cache": stats.get("prefix_cache"), "level_generation": stats.get("level_generation", 0)}
    return out


class Service:
    def __init__(self, store, *, auth: CoreAuth, host: str, model_gateway=None, jev_url=None):
        self.store, self.auth, self.host = store, auth, host
        self.model_gateway, self.jev_url = model_gateway, jev_url
        self.sink = None
        self.review_clock = time.time
        try:
            self.review_grace_s = float(os.environ.get("AX_PUBLISH_REVIEW_GRACE_S", "60"))
        except ValueError:
            self.review_grace_s = 0

    def bind_loop(self):
        return None


def build_routes(svc: Service, *, work_side: bool, review_side: bool = False):
    async def get_embedding_registration(request: Request):
        q = request.query_params
        try:
            if "endpoint" in q:
                registered = svc.store.registered_embedding_endpoint(q.get("provider"), q.get("model"), q.get("endpoint"))
            else:
                registered = svc.store.registered_model(q.get("provider"), q.get("model"))
        except Exception:
            registered = False
        return JSONResponse({"registered": registered is True})

    async def get_resource(request: Request):
        r = svc.store.get(request.path_params["resource_id"])
        if r is None:
            return json_error(404, "not_found", "その ID の資源がない")
        return JSONResponse(_public(svc.store.apply_command_exclusions([r])[0]))

    async def list_resources(request: Request):
        q = request.query_params
        rows = svc.store.list(kind=q.get("kind"), family=q.get("family"), tier=q.get("tier"),
                              limit=2147483647 if q.get("status") else 500)
        rows = svc.store.apply_command_exclusions(rows)
        if q.get("status"):
            rows = [row for row in rows if row["status"] == q["status"]][:500]
        return JSONResponse({"resources": [_public(x) for x in rows]})

    async def sync_command_exclusion(request: Request):
        cred, result = svc.auth.check(request.headers.get("authorization"), "command-exclusion")
        if result != "ok":
            return json_error(401 if cred is None else 403, result, result)
        body, err = await read_json(request)
        if err:
            return err
        if not isinstance(body, dict) or any(not isinstance(body.get(k), str) or not body[k] for k in ("command_id", "resource_id")) or type(body.get("active")) is not bool:
            return json_error(400, "bad_request", "invalid command exclusion")
        if body["active"] and any(not isinstance(body.get(k), str) or not body[k] for k in ("reason", "until")):
            return json_error(400, "bad_request", "invalid command exclusion")
        try:
            found = await asyncio.to_thread(svc.store.sync_command_exclusion, body["command_id"], body["resource_id"], active=body["active"],
                                                      reason=body.get("reason", ""), until=body.get("until", ""))
        except ValueError:
            return json_error(409, "command_resource_mismatch", "command resource mismatch")
        if not found:
            return json_error(404, "not_found", "resource not found")
        return JSONResponse({"ok": True})

    async def post_outcome(request: Request):
        body, err = await read_json(request)
        if err:
            return err
        rid = request.path_params["resource_id"]
        if svc.store.get(rid) is None:
            return json_error(404, "not_found", "その ID の資源がない")
        b = body or {}
        if not isinstance(b, dict):
            return json_error(400, "bad_request", "invalid outcome")
        job_id, trial_id = b.get("job_id"), b.get("trial_id")
        if job_id is None:
            job_id = trial_id
        if trial_id is None:
            trial_id = job_id
        if not all(isinstance(v, str) and v and len(v) <= 200 for v in (job_id, trial_id)):
            return json_error(400, "bad_request", "job_id or trial_id required")
        outcome, caf, rc = b.get("outcome", "pass"), b.get("counts_as_failure", "hold"), b.get("verify_rc")
        generation = b.get("level_generation")
        pa, da = b.get("promote_after", 3), b.get("demote_after", 2)
        if (outcome not in ("pass", "fail") or caf not in ("yes", "no", "hold")
                or (rc is not None and type(rc) is not int)
                or (generation is not None and (type(generation) is not int or generation < 0))
                or type(pa) is not int or not 2 <= pa <= 10 or type(da) is not int or not 1 <= da <= 5
                or (b.get("error_class") is not None and not isinstance(b.get("error_class"), str))):
            return json_error(400, "bad_request", "invalid outcome")
        out = svc.store.record_evaluated_outcome(rid, outcome=outcome, verify_rc=rc,
            error_class=b.get("error_class"), counts_as_failure=caf, job_id=job_id, trial_id=trial_id,
            level_generation=generation, promote_after=pa, demote_after=da)
        if out is None:
            return json_error(404, "not_found", "resource not found")
        return JSONResponse({"ok": True, "resource": _public(out)})

    async def post_exclude(request: Request):
        body, err = await read_json(request)
        if err:
            return err
        rid = request.path_params["resource_id"]
        b = body or {}
        out = svc.store.exclude(rid, b.get("reason", "manual"), b.get("until"), keep_status=bool(b.get("until")))
        if out is None:
            return json_error(404, "not_found", "その ID の資源がない")
        return JSONResponse({"ok": True, "resource": _public(out)})

    async def post_include(request: Request):
        body, err = await read_json(request)
        if err:
            return err
        rid = request.path_params["resource_id"]
        b = body or {}
        out = svc.store.release_exclusions(rid, reason=b.get("reason"), until=b.get("until"))
        if out is None:
            return json_error(404, "not_found", "その ID の資源がない")
        return JSONResponse({"ok": True, "resource": _public(out)})

    async def post_prefix_cache(request: Request):
        # 呼べるのは worker-pool の資格だけ（D-7.3）。core-tokens で検証する
        cred, result = svc.auth.check(request.headers.get("authorization"), "gate-token")
        if result != "ok":
            if cred is None:
                return json_error(401, "unauthenticated", "資格が一覧にない")
            return json_error(403, "credential_scope", "この API を呼べない資格")
        body, err = await read_json(request)
        if err:
            return err
        v = (body or {}).get("value")
        if v not in ("on", "off", "unknown"):
            return json_error(400, "bad_request", "value は on / off / unknown のどれか")
        rid = request.path_params["resource_id"]
        out = svc.store.set_prefix_cache(rid, v, ratio=(body or {}).get("ratio"), measured_at=(body or {}).get("measured_at"))
        if out is None:
            return json_error(404, "not_found", "その ID の資源がない")
        return JSONResponse({"ok": True, "resource": _public(out)})

    async def post_secret_push(request: Request):
        _, result = svc.auth.check(request.headers.get("authorization"), "secret-push")
        if result != "ok":
            return json_error(401 if result == "unauthenticated" else 403, result, result)
        body, err = await read_json(request)
        if err:
            return err
        if not isinstance(body, dict) or set(body) != {"slot"} or not isinstance(body["slot"], str):
            return json_error(422, "bad_request", "bad_request")
        try:
            import asyncio
            await asyncio.to_thread(svc.secret_push, body["slot"])
        except Exception:
            return json_error(503, "secret_push_failed", "secret_push_failed")
        return JSONResponse({"ok": True})

    def slot_auth(request: Request, scope: str):
        cred, result = svc.auth.check(request.headers.get("authorization"), scope)
        if result == "ok":
            return None
        return json_error(401 if cred is None else 403, "unauthenticated" if cred is None else "credential_scope", "credential not permitted")

    async def put_model_runners(request: Request):
        if err := slot_auth(request, "model-runners"):
            return err
        body, err = await read_json(request)
        if err:
            return err
        if not isinstance(body, dict) or set(body) != {"model", "runners"}:
            return json_error(422, "bad_request", "bad_request")
        try:
            result = svc.store.set_supported_runners(body["model"], body["runners"])
        except (ValueError, TypeError):
            return json_error(422, "bad_request", "bad_request")
        except KeyError:
            return json_error(404, "not_found", "not_found")
        return JSONResponse({"ok": True, "resource": _public(result)})

    async def get_slots(request: Request):
        return JSONResponse(svc.store.slots())

    async def put_cap(request: Request):
        if err := slot_auth(request, "slot-cap"):
            return err
        body, err = await read_json(request)
        if err:
            return err
        if not isinstance(body, dict) or set(body) != {"kind", "key", "cap"}:
            return json_error(400, "bad_request", "kind, key and cap required")
        try:
            svc.store.set_cap(body["kind"], body["key"], body["cap"])
        except (TypeError, ValueError):
            return json_error(400, "invalid_cap", "invalid cap, key or kind")
        except KeyError:
            return json_error(404, "not_found", "model not found")
        return JSONResponse({"ok": True})

    async def get_effective_limits_revision(request: Request):
        if err := slot_auth(request, "slot-cap"):
            return err
        return JSONResponse({"revision": svc.store.effective_limits_revision()})

    async def put_effective_limits(request: Request):
        if err := slot_auth(request, "slot-cap"):
            return err
        body, err = await read_json(request)
        if err:
            return err
        if not isinstance(body, dict) or set(body) != {"revision", "expires_at", "models", "families"}:
            return json_error(400, "bad_request", "revision, expires_at, models and families required")
        try:
            svc.store.set_effective_limits(body["revision"], body["expires_at"], body["models"], body["families"])
        except ValueError as exc:
            if str(exc) == "revision_conflict":
                return json_error(409, "revision_conflict", "revision is stale or conflicting")
            return json_error(400, "invalid_limits", "invalid effective limits")
        return JSONResponse({"ok": True})

    async def reserve_slot(request: Request):
        if err := slot_auth(request, "slot-reserve"):
            return err
        body, err = await read_json(request)
        if err:
            return err
        if not isinstance(body, dict) or set(body) != {"reservation_id", "model"} or not isinstance(body["model"], str):
            return json_error(400, "bad_request", "reservation_id and model required")
        if (svc.auth.identify(request.headers.get("authorization")) == "release-review"
                and (not isinstance(body["reservation_id"], str) or not body["reservation_id"].startswith("publish-review-"))):
            return json_error(403, "credential_scope", "credential not permitted")
        try:
            ok, reason = svc.store.reserve_slot_with_reason(body["reservation_id"], body["model"])
        except ValueError:
            return json_error(409, "reservation_conflict", "invalid or conflicting reservation")
        except KeyError:
            return json_error(404, "not_found", "model not found")
        return JSONResponse({"reserved": ok, **({"reason": reason} if reason else {})}, status_code=200 if ok else 409)

    async def reassign_slot(request: Request):
        if err := slot_auth(request, "slot-reserve"):
            return err
        body, err = await read_json(request)
        if err:
            return err
        if not isinstance(body, dict) or set(body) != {"reservation_id", "previous_model", "model"} or not all(isinstance(v, str) and v for v in body.values()):
            return json_error(400, "bad_request", "reservation and models required")
        if svc.auth.identify(request.headers.get("authorization")) != "worker-pool":
            return json_error(403, "credential_scope", "credential not permitted")
        try:
            ok = svc.store.reassign_slot(body["reservation_id"], body["previous_model"], body["model"])
        except ValueError:
            return json_error(409, "reservation_conflict", "conflicting reservation")
        except KeyError:
            return json_error(404, "not_found", "model not found")
        return JSONResponse({"reserved": ok}, status_code=200 if ok else 409)

    async def release_slot(request: Request):
        if err := slot_auth(request, "slot-reserve"):
            return err
        body, err = await read_json(request)
        if err:
            return err
        if not isinstance(body, dict) or set(body) not in ({"reservation_id"}, {"reservation_id", "outcome"}) or not isinstance(body.get("reservation_id"), str) or not body["reservation_id"]:
            return json_error(400, "bad_request", "reservation_id required")
        credential = svc.auth.identify(request.headers.get("authorization"))
        if credential == "release-review" and not body["reservation_id"].startswith("publish-review-"):
            return json_error(403, "credential_scope", "credential not permitted")
        if "outcome" in body:
            if credential != "release-review" or body["outcome"] != "unknown":
                return json_error(403, "credential_scope", "credential not permitted")
            try:
                marked = svc.store.mark_review_unknown(body["reservation_id"], now=svc.review_clock(), grace=svc.review_grace_s)
            except ValueError:
                return json_error(503, "review_grace_unavailable", "review grace unavailable")
            return JSONResponse({"unknown": marked}, status_code=200 if marked else 404)
        return JSONResponse({"released": svc.store.release_slot(body["reservation_id"])})

    async def reconcile_reviews(request: Request):
        credential, result = svc.auth.check(request.headers.get("authorization"), "slot-reserve")
        if result != "ok" or credential != "worker-pool":
            return json_error(401 if credential is None else 403, "credential_scope", "credential not permitted")
        return JSONResponse({"released": svc.store.reconcile_review_slots(now=svc.review_clock())})

    async def review_outcome(request: Request):
        credential, result = svc.auth.check(request.headers.get("authorization"), "slot-reserve")
        if result != "ok" or credential != "worker-pool":
            return json_error(401 if credential is None else 403, "credential_scope", "credential not permitted")
        outcome = svc.store.review_slot_outcome(request.path_params["reservation_id"])
        if outcome is None:
            return json_error(404, "not_found", "review outcome not found")
        return JSONResponse(outcome)

    async def review_reserve(request: Request):
        credential, result = svc.auth.check(request.headers.get("authorization"), "slot-reserve")
        if result != "ok" or credential != "release-review":
            return json_error(401 if credential is None else 403, "credential_scope", "credential not permitted")
        return await reserve_slot(request)

    async def review_release(request: Request):
        credential, result = svc.auth.check(request.headers.get("authorization"), "slot-reserve")
        if result != "ok" or credential != "release-review":
            return json_error(401 if credential is None else 403, "credential_scope", "credential not permitted")
        return await release_slot(request)
    async def acquire_environment(request: Request):
        if err := slot_auth(request, "environment-lease"):
            return err
        body, err = await read_json(request)
        if err:
            return err
        if not isinstance(body, dict) or set(body) != {"env_id", "holder", "kind"}:
            return json_error(400, "bad_request", "env_id, holder and kind required")
        try:
            lease_id = svc.store.acquire(body["env_id"], body["holder"], body["kind"])
        except (TypeError, ValueError):
            return json_error(400, "bad_request", "invalid lease")
        except KeyError:
            return json_error(409, "resource_conflict", "resource is not an environment")
        return JSONResponse({"lease_id": lease_id}, status_code=200 if lease_id else 409)

    async def release_environment(request: Request):
        if err := slot_auth(request, "environment-lease"):
            return err
        body, err = await read_json(request)
        if err:
            return err
        if not isinstance(body, dict) or set(body) != {"lease_id", "outcome"}:
            return json_error(400, "bad_request", "lease_id and outcome required")
        try:
            released = svc.store.release(body["lease_id"], body["outcome"])
        except (TypeError, ValueError):
            return json_error(400, "bad_request", "invalid release")
        return JSONResponse({"released": released})

    async def hold_unknown_environment(request: Request):
        if err := slot_auth(request, "environment-lease"):
            return err
        body, err = await read_json(request)
        if err:
            return err
        if not isinstance(body, dict) or set(body) != {"lease_id"}:
            return json_error(400, "bad_request", "lease_id required")
        try:
            held = svc.store.hold_unknown(body["lease_id"])
        except (TypeError, ValueError):
            return json_error(400, "bad_request", "invalid lease")
        return JSONResponse({"held": held})

    async def get_ledger(request: Request):
        q = request.query_params
        return JSONResponse({"ledger": svc.store.ledger(model=q.get("model"), role=q.get("role"), since=q.get("since"))})

    async def get_discovery(request: Request):
        d = svc.store.last_discovery()
        if d is None:
            return JSONResponse({"fetched_at": None, "ok": False, "error": "not_fetched"})
        return JSONResponse(d)

    async def get_selections(request: Request):
        return JSONResponse({"selections": svc.store.selections(job_id=request.query_params.get("job_id"))})

    async def get_status(request: Request):
        return JSONResponse({"mode": "run", "knobs": {}, "running": [], "queued": 0})

    async def post_apply(request: Request):
        return JSONResponse({"ok": True, "ignored": False})

    if review_side:
        return [Route("/v1/slots/reserve", review_reserve, methods=["POST"]),
                Route("/v1/slots/release", review_release, methods=["POST"])]
    if work_side:
        return [
            Route("/v1/embedding-registration", get_embedding_registration, methods=["GET"]),
            Route("/v1/resources", list_resources, methods=["GET"]),
            Route("/v1/resources/{resource_id:path}", get_resource, methods=["GET"]),
            Route("/status", get_status, methods=["GET"]),
        ]
    return [
        Route("/v1/embedding-registration", get_embedding_registration, methods=["GET"]),
        Route("/v1/model-runners", put_model_runners, methods=["PUT"]),
        Route("/v1/slots", get_slots, methods=["GET"]),
        Route("/v1/slots/cap", put_cap, methods=["PUT"]),
        Route("/v1/slots/effective-limits", get_effective_limits_revision, methods=["GET"]),
        Route("/v1/slots/effective-limits", put_effective_limits, methods=["PUT"]),
        Route("/v1/environment-leases/acquire", acquire_environment, methods=["POST"]),
        Route("/v1/environment-leases/release", release_environment, methods=["POST"]),
        Route("/v1/environment-leases/hold-unknown", hold_unknown_environment, methods=["POST"]),
        Route("/v1/slots/reserve", reserve_slot, methods=["POST"]),
        Route("/v1/slots/reassign", reassign_slot, methods=["POST"]),
        Route("/v1/slots/release", release_slot, methods=["POST"]),
        Route("/v1/slots/reconcile-reviews", reconcile_reviews, methods=["POST"]),
        Route("/v1/slots/review-outcomes/{reservation_id}", review_outcome, methods=["GET"]),
        Route("/v1/resources/{resource_id:path}", get_resource, methods=["GET"]),
        Route("/v1/resources", list_resources, methods=["GET"]),
        Route("/v1/command-exclusions", sync_command_exclusion, methods=["POST"]),
        Route("/v1/resources/{resource_id:path}/outcome", post_outcome, methods=["POST"]),
        Route("/v1/resources/{resource_id:path}/exclude", post_exclude, methods=["POST"]),
        Route("/v1/resources/{resource_id:path}/include", post_include, methods=["POST"]),
        Route("/v1/resources/{resource_id:path}/prefix_cache", post_prefix_cache, methods=["POST"]),
        Route("/v1/secret-push", post_secret_push, methods=["POST"]),
        Route("/v1/ledger", get_ledger, methods=["GET"]),
        Route("/v1/models/discovery", get_discovery, methods=["GET"]),
        Route("/v1/selections", get_selections, methods=["GET"]),
        Route("/status", get_status, methods=["GET"]),
        Route("/apply", post_apply, methods=["POST"]),
    ]
