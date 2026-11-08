"""Authenticated internal release delivery endpoints."""
from __future__ import annotations

import asyncio
import hmac

from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon.creds import parse_bearer
from axcommon.http import json_error, read_json
from .registry import Held


def delivery_routes(service, *, change_token, apply_token=None):
    async def handle(request, lookup=False):
        supplied = parse_bearer(request.headers.get('authorization'))
        if (not change_token or not supplied or
                (apply_token and hmac.compare_digest(change_token, apply_token)) or
                not hmac.compare_digest(supplied, change_token)):
            return json_error(401, 'unauthorized', 'change credential required')
        if service is None:
            return json_error(503, 'release_unavailable', 'release service unavailable')
        data, err = await read_json(request)
        if err is not None:
            return err
        expected = {'delivery_id', 'destination'} | (set() if lookup else {'payload'})
        if not isinstance(data, dict) or set(data) != expected:
            return json_error(422, 'bad_request', 'invalid delivery request')
        try:
            if lookup:
                result = await asyncio.to_thread(service.lookup_delivery, data['delivery_id'], data['destination'])
            else:
                result = await asyncio.to_thread(service.deliver, data['delivery_id'], data['destination'], data['payload'])
        except Held as exc:
            code = str(exc)
            status = 409 if code in {'delivery_identity_conflict', 'release_item_conflict'} else 422
            return json_error(status, code, 'delivery rejected')
        except (KeyError, TypeError, ValueError, OverflowError):
            return json_error(422, 'bad_request', 'invalid delivery request')
        return JSONResponse(result)

    async def deliver(request):
        return await handle(request)

    async def lookup(request):
        return await handle(request, lookup=True)

    return [Route('/release/deliveries', deliver, methods=['POST']),
            Route('/release/deliveries/lookup', lookup, methods=['POST'])]
