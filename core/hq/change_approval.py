"""Component-only change approval relay into the HQ intake socket."""
from __future__ import annotations

import json

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon.change_approval import CHECK_PATH, REQUEST_PATH, component_proof, validate_scope
from axcommon.hq_proof import KeyUnavailable
from core.hq.api import error, object_body
from core.hq.api.service import InvalidRequest
from core.hq.notify.intake import unix_transport
from core.hq.notify.receipt_auth import authorize_component


def routes(*, transport=None):
    transport = transport or unix_transport()

    async def endpoint(request):
        denied = authorize_component(request, request.app.state.hq_settings)
        if denied:
            return error(denied, 'component_forbidden')
        try:
            body = await object_body(request)
            path = REQUEST_PATH if request.url.path == '/v1/change/approval-requests' else CHECK_PATH
            keys = {'request_id', 'scope'} if path == REQUEST_PATH else {'approval_id', 'scope'}
            if set(body) != keys:
                raise ValueError('invalid_request')
            validate_scope(body['scope'])
            proof = component_proof(path, body)
            status, raw = await run_in_threadpool(transport, 'POST', path, json.dumps(body, ensure_ascii=False).encode(),
                                                  {'X-AX-Change-Proof': proof})
            result = json.loads(raw) if raw else {}
            if not isinstance(result, dict):
                raise ValueError('invalid_response')
            return JSONResponse(result, status_code=status)
        except KeyUnavailable:
            return error(503, 'approval_unavailable')
        except (InvalidRequest, ValueError, TypeError, KeyError):
            return error(422, 'bad_request')
        except Exception:
            return error(503, 'approval_unavailable')

    return [Route('/v1/change/approval-requests', endpoint, methods=['POST']),
            Route('/v1/change/approval-check', endpoint, methods=['POST'])]
