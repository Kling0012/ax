"""Intake authority for change's record-only approval requests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon import ids, timeutil
from axcommon.change_approval import CHECK_PATH, DESTINATION, REQUEST_PATH, validate_scope, verify_component_proof
from core.intake.vias import Ident
from .store import ApprovalError, canonical_json, payload_digest


def register_change_approval(app, service):
    async def endpoint(request):
        ident = request.scope.get('ax_ident')
        if not isinstance(ident, Ident) or (ident.via, ident.from_, ident.principal) != ('hq', 'hq', 'user'):
            return JSONResponse({'error': {'code': 'forbidden'}}, status_code=403)
        try:
            raw = await request.body()
            if len(raw) > 16384:
                raise ValueError('body_too_large')
            body = json.loads(raw)
            headers = request.headers.getlist('x-ax-change-proof')
            if len(headers) != 1 or not verify_component_proof(request.url.path, body, headers[0]):
                return JSONResponse({'error': {'code': 'forbidden'}}, status_code=403)
            scope = validate_scope(body.get('scope'))
            if request.url.path == REQUEST_PATH:
                if set(body) != {'request_id', 'scope'} or not isinstance(body['request_id'], str) or not 1 <= len(body['request_id']) <= 200:
                    raise ValueError('invalid_request')
                response = create(service, body['request_id'], scope)
            else:
                if set(body) != {'approval_id', 'scope'} or not isinstance(body['approval_id'], str) or not ids.is_ulid(body['approval_id']):
                    raise ValueError('invalid_request')
                response = check(service, body['approval_id'], scope)
            return JSONResponse(response)
        except ApprovalError as exc:
            return JSONResponse({'error': {'code': exc.reason}}, status_code=exc.status)
        except (ValueError, TypeError, KeyError, UnicodeError):
            return JSONResponse({'error': {'code': 'bad_request'}}, status_code=422)
        except Exception:
            return JSONResponse({'error': {'code': 'unavailable'}}, status_code=503)
    app.router.routes.extend([Route(REQUEST_PATH, endpoint, methods=['POST']), Route(CHECK_PATH, endpoint, methods=['POST'])])


def create(service, request_id, scope):
    now = datetime.now(timezone.utc)
    expires = datetime.fromisoformat(scope['expires_at'].replace('Z', '+00:00'))
    if expires <= now or expires > now + timedelta(minutes=service.ttl_min):
        raise ApprovalError(422, 'expiry_invalid')
    if service.audit.redactor.scan(canonical_json(scope)) or service.audit.redactor.scan(request_id):
        raise ApprovalError(422, 'scan_blocked')
    digest = payload_digest(scope)
    created = False
    with service.store.transaction():
        for row in service.store.list():
            result = json.loads(row['result_json'])
            if row['destination'] == DESTINATION and result.get('request_key') == request_id:
                if row['digest'] != digest or row['payload_json'] != canonical_json(scope):
                    raise ApprovalError(409, 'idempotency_conflict')
                aid = row['approval_id']
                break
        else:
            aid = ids.new_ulid()
            result = {'request_key': request_id, 'caller': {'via': 'hq', 'tier': 'irreversible', 'component': 'change'}, 'card_pending': True}
            row = {'approval_id': aid, 'destination': DESTINATION, 'payload_json': canonical_json(scope),
                   'digest': digest, 'requested_by': 'component:change', 'session': None, 'project': scope['project'],
                   'created_at': timeutil.now_ts(), 'expires_at': scope['expires_at'], 'state': 'pending_user',
                   'granted_at': None, 'executed_at': None, 'result_json': canonical_json(result)}
            service.store.insert(row)
            created = True
    if created:
        service._event('approval.requested', row, Ident('hq', 'hq', 'component:change', 'irreversible'))
    if row['state'] == 'pending_user':
        service.deliver_card(aid)
    return {'approval_id': aid, 'status': row['state']}


def check(service, aid, scope):
    service.refresh(aid)
    row = service.store.get(aid)
    if row['destination'] != DESTINATION or row['payload_json'] != canonical_json(scope) or row['digest'] != payload_digest(scope):
        raise ApprovalError(409, 'scope_mismatch')
    expiry = datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00'))
    if expiry <= datetime.now(timezone.utc):
        status = 'expired'
    elif row['state'] == 'executed' and row['granted_at'] and row['executed_at'] and json.loads(row['result_json']).get('execution') == 'completed':
        status = 'approved'
    elif row['state'] == 'pending_user':
        status = 'pending'
    elif row['state'] in {'denied', 'expired', 'withdrawn'}:
        status = row['state']
    else:
        status = 'unknown'
    return {'approval_id': aid, 'scope': scope, 'status': status}
