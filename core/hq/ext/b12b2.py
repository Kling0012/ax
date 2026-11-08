"""b12b2 register(app): HQ pages only; no listeners or background jobs."""
from __future__ import annotations

import inspect
import json
import os
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from axcommon.client import ApiClient, service_url
from core.control import SENDING_OUTPUTS
from starlette.routing import Route

from axcommon.defaults import Defaults
from core.hq.approval.intake import UnixIntake
from core.hq.approval.pages import Pages
from core.hq.approval.private import PrivateStore
from core.hq.approval.qr import svg
from core.hq.approval import render
from core.hq.approval.service import ApprovalFailure, FactorService, Identity, VerifiedAction
from core.hq.approval.security import BrowserBoundary, BoundaryError
from core.hq.seats.service import FACTOR_OPERATIONS, SeatService
from core.hq.seats.render import seat_page, confirmation_page, completed_page


@dataclass(frozen=True)
class BrowserPolicy:
    hosts: frozenset[str]
    origins: frozenset[str]


def _headers(response):
    response.headers.update({"Cache-Control": "no-store, private", "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
    return response


async def _body(request):
    # Bounded streaming also handles absent/forged Content-Length.
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > 65536:
            raise ApprovalFailure(413, "too_large")
    try:
        kind = request.headers.get('content-type', '').split(';')[0]
        if kind == 'application/json':
            value = json.loads(chunks)
        elif kind == 'application/x-www-form-urlencoded':
            values = parse_qs(chunks.decode('utf-8'), keep_blank_values=True, max_num_fields=30)
            if any(len(v) != 1 for v in values.values()):
                raise ValueError()
            value = {k: v[0] for k, v in values.items()}
        else:
            raise ValueError()
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise ApprovalFailure(422, "bad_request") from None


def _only(body, allowed):
    if set(body) - set(allowed):
        raise ApprovalFailure(422, "bad_request")


def _row(value):
    data = value.get('data', value)
    if not isinstance(data, dict):
        raise ApprovalFailure(502, "intake_bad_response")
    return data


class HQApproval:
    def __init__(self, app, intake, factor, pages):
        self.app, self.intake, self.factor, self.pages = app, intake, factor, pages
        self.seats = SeatService(intake, registration_display=getattr(app.state, 'hq_seat_registration_display', None))

    async def identity(self, request):
        policy = getattr(self.app.state, 'hq_approval_browser_policy', None)
        boundary = BrowserBoundary(policy.hosts, policy.origins) if isinstance(policy, BrowserPolicy) else BrowserBoundary()
        try:
            who = await boundary.authorize(request)
        except BoundaryError as exc:
            raise ApprovalFailure(exc.status, exc.reason) from None
        if request.method == 'POST':
            origin = urlsplit(request.headers['origin'])
            if origin.netloc != request.headers['host'] or origin.scheme not in {'http', 'https'}:
                raise ApprovalFailure(403, 'origin_not_allowed')
        return who

    async def handle(self, request):
        try:
            who = await self.identity(request)
            proof = VerifiedAction(who, False)
            path = request.url.path
            if path == '/v1/hq/b12b2/ui.js':
                return _headers(Response(render.JS, media_type='application/javascript'))
            if path == '/v1/hq/b12b2/ui.css':
                return _headers(Response(render.CSS, media_type='text/css'))
            if path == '/v1/hq/approval-snapshot':
                value = await run_in_threadpool(self.intake.approval_snapshot, proof)
                if not isinstance(value, dict) or not isinstance(value.get('approvals'), list):
                    raise ApprovalFailure(502, 'intake_bad_response')
                return _headers(JSONResponse(value))
            if path == '/v1/hq/plugins':
                control = getattr(self.app.state, 'hq_dashboard_control', None) or ApiClient(service_url('control'))
                value = await run_in_threadpool(control.get, '/v1/plugins')
                if not isinstance(value, dict) or not isinstance(value.get('plugins'), list):
                    raise ApprovalFailure(502, 'control_bad_response')
                plugins = []
                for item in value['plugins']:
                    if not isinstance(item, dict) or not isinstance(item.get('manifest'), dict):
                        raise ApprovalFailure(502, 'control_bad_response')
                    sending = bool(set(item['manifest'].get('outputs') or []) & SENDING_OUTPUTS)
                    if item.get('kind') == 'timer' or sending:
                        plugins.append({k: item.get(k) for k in ('id', 'kind', 'status', 'mode')} | {'sending': sending})
                return _headers(JSONResponse({'plugins': plugins}))
            if request.method == 'GET':
                if path == '/seats':
                    seats, approvals = self.intake.seats(proof), self.intake.approvals(proof)
                    # A trusted response presenter can map the still-unspecified
                    # lock/denial response envelope without inventing wire keys.
                    present = getattr(self.app.state, 'hq_seats_presenter', None)
                    if present:
                        seats = present(seats)
                    page = self.pages.issue(who, 'seat.prepare', {})
                    text = seat_page(seats, approvals, self.factor.status(), page)
                elif path == '/approval/settings':
                    page = self.pages.issue(who, 'factor.enroll', {})
                    text = render.factor_page(self.factor.status(), page)
                else:
                    aid = request.path_params['approval_id']
                    saved = _row(self.intake.approval(aid, proof))
                    if saved.get('approval_id') != aid:
                        raise ApprovalFailure(502, 'intake_bad_response')
                    page = self.pages.issue(who, 'approval:' + aid, {'digest': saved['digest']})
                    text = render.approval_page(saved, page)
                return _headers(HTMLResponse(text))
            body = await _body(request)
            common = {'page_id', 'csrf_token', 'code'}
            if path == '/v1/hq/approval/enroll':
                _only(body, common)
                self.pages.consume(who, 'factor.enroll', body)
                result = self.factor.enroll(who, body.get('code'))
                return _headers(HTMLResponse(render.qr_page(result)))
            if path == '/v1/hq/seats/prepare':
                if 'payload' in body:
                    _only(body, common | {'destination', 'payload'})
                    payload = body['payload']
                else:
                    _only(body, common | {'destination', 'label', 'tier', 'seat_id', 'expires_at', 'scope'})
                    keys = {'seat.enroll_start': ('label', 'tier'), 'seat.tier': ('seat_id', 'tier', 'expires_at'),
                            'seat.revoke': ('seat_id',), 'seat.lock': ('scope',), 'seat.unlock': ('scope',)}
                    payload = {k: body[k] for k in keys.get(body.get('destination'), ()) if body.get(k)}
                self.pages.consume(who, 'seat.prepare', body)
                saved = self.seats.prepare(body.get('destination'), payload)
                page = self.pages.issue(who, 'seat.apply', saved)
                return _headers(HTMLResponse(confirmation_page(saved, page)))
            if path == '/v1/hq/seats/apply':
                _only(body, common)
                saved = self.pages.consume(who, 'seat.apply', body)
                if saved['destination'] in FACTOR_OPERATIONS:
                    proof = self.factor.verify(who, body.get('code'))
                status, display = self.seats.apply(saved, proof)
                return _headers(HTMLResponse(completed_page(saved['destination'], display), status_code=status))
            _only(body, common)
            aid = request.path_params['approval_id']
            saved = self.pages.consume(who, 'approval:' + aid, body)
            proof = self.factor.verify(who, body.get('code'))
            if path.endswith('/grant'):
                status, _ = self.intake.grant(aid, saved['digest'], proof)
            else:
                status, _ = self.intake.deny(aid, saved['digest'], proof,
                                             request_id='hq-deny:' + body['page_id'])
            return _headers(HTMLResponse(render.shell('承認の結果', '<p>入口へ操作を送った。</p>'), status_code=status))
        except ApprovalFailure as exc:
            return _headers(JSONResponse({'error': {'code': exc.code, 'message': exc.code, 'hint': ''}}, status_code=exc.status))
        except NotImplementedError:
            return _headers(JSONResponse({'error': {'code': 'hq_approval_binding_missing', 'message': 'hq_approval_binding_missing', 'hint': ''}}, status_code=501))
        except (ConnectionError, OSError):
            return _headers(JSONResponse({'error': {'code': 'approval_unavailable', 'message': 'approval_unavailable', 'hint': ''}}, status_code=503))
        except (ValueError, KeyError, TypeError):
            return _headers(JSONResponse({'error': {'code': 'approval_bad_state', 'message': 'approval_bad_state', 'hint': ''}}, status_code=503))

        except Exception:
            if request.url.path not in {'/v1/hq/approval-snapshot', '/v1/hq/plugins'}:
                raise
            return _headers(JSONResponse({'error': {'code': 'component_unavailable'}}, status_code=503))


def register(app):
    intake = getattr(app.state, 'hq_approval_intake', None)
    if intake is None:
        from axcommon.hq_proof import encode
        intake = UnixIntake(proof_encoder=getattr(app.state, 'hq_approval_proof_encoder', None) or encode)
    factor = getattr(app.state, 'hq_approval_factor', None)
    if factor is None:
        defaults = Defaults.load()
        adapters = getattr(app.state, 'hq_factor_adapters', None)
        if adapters is None and not (getattr(app.state, 'hq_factor_emit', None) or getattr(app.state, 'hq_factor_notify', None)):
            from core.hq.notify.factor import FactorDelivery
            hub = getattr(app.state, 'notify', None)
            sink = getattr(app.state, 'sink', None)
            dispatcher = getattr(hub, 'dispatcher', None)
            if sink is not None and dispatcher is not None:
                adapters = FactorDelivery(sink, dispatcher)
        store = getattr(app.state, 'hq_factor_private_store', None) or PrivateStore('/run/hq/private')
        factor = FactorService(store,
            fail_max=defaults.get('approval_code_fail_max'), lock_min=defaults.get('approval_lock_min'),
            audit_window_s=defaults.get('audit_window_s'),
            emit=getattr(adapters, 'emit', None) if adapters else getattr(app.state, 'hq_factor_emit', None),
            notify=getattr(adapters, 'notify', None) if adapters else getattr(app.state, 'hq_factor_notify', None), qr_renderer=svg)
    controller = HQApproval(app, intake, factor, Pages())
    app.state.hq_approval = controller
    for path, methods in [('/v1/hq/approval-snapshot', ['GET']), ('/v1/hq/plugins', ['GET']), ('/seats', ['GET']), ('/approval/settings', ['GET']),
            ('/approvals/{approval_id}', ['GET']), ('/v1/hq/approvals/{approval_id}/grant', ['POST']),
            ('/v1/hq/approvals/{approval_id}/deny', ['POST']), ('/v1/hq/approval/enroll', ['POST']),
            ('/v1/hq/seats/prepare', ['POST']), ('/v1/hq/seats/apply', ['POST']),
            ('/v1/hq/b12b2/ui.js', ['GET']), ('/v1/hq/b12b2/ui.css', ['GET'])]:
        app.router.routes.append(Route(path, controller.handle, methods=methods))
