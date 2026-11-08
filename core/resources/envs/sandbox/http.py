"""Core-side Starlette routes for the sandbox token service."""
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.concurrency import run_in_threadpool

from .gate import GateDenied


async def _body(request: Request):
    try:
        value = await request.json()
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def build_routes(gate):
    async def issue(request):
        body = await _body(request)
        try:
            issued = await run_in_threadpool(gate.issue, request.path_params['env_id'], request.headers.get('authorization'), body)
            return JSONResponse({'token_id': issued.token_id, 'token': issued.token,
                                 'expires_at': issued.expires_at, 'gate_path': issued.gate_path}, status_code=201,
                                headers={'Cache-Control': 'no-store'})
        except GateDenied as exc:
            status = 401 if exc.reason == 'unauthenticated' else 422 if exc.reason == 'bad_request' else 409 if exc.reason in {'slot_busy', 'token_exists'} else 403
            return JSONResponse({'error': {'code': exc.reason}}, status_code=status)

    async def revoke(request):
        # Authenticate before parsing to avoid a distinct unauthenticated response.
        try:
            gate._authenticate(request.headers.get('authorization'))
            body = await _body(request)
            if body is None or set(body) != {'reason'} or not isinstance(body['reason'], str) or body['reason'] not in {'finished', 'cancelled', 'failed'}:
                return JSONResponse({'error': {'code': 'bad_request'}}, status_code=422)
            changed = await run_in_threadpool(gate.revoke, request.path_params['env_id'], request.path_params['token_id'], request.headers.get('authorization'))
            return JSONResponse({'already': not changed})
        except GateDenied as exc:
            return JSONResponse({'error': {'code': exc.reason}}, status_code=401 if exc.reason == 'unauthenticated' else 404)

    return [Route('/v1/envs/{env_id}/gate-token', issue, methods=['POST']),
            Route('/v1/envs/{env_id}/gate-token/{token_id}/revoke', revoke, methods=['POST'])]
