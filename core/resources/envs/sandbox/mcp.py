"""Sandbox MCP HTTP endpoint. All provider operations pass the shared policy."""
from __future__ import annotations

import json
import logging

from core.resources.box.protocol import BoxError

_LOG = logging.getLogger(__name__)
_DENIAL_CODES = frozenset({"sensitivity_denied", "repo_not_public", "scan_blocked",
                           "forbidden_command", "tool_denied", "credential_invalid",
                           "credential_missing"})

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


class SandboxMCP:
    def __init__(self, *, gate, policy, resolve_job, repo_is_public, implementations, invoke):
        """implementations maps environment IDs to verified common tool names.

        invoke(env_id, provider_mapping, arguments) must use a worker-only key.
        resolve_job and the implementations mapping are trusted host configuration.
        """
        self.gate = gate
        self.policy = policy
        self.resolve_job = resolve_job
        self.repo_is_public = repo_is_public
        self.implementations = implementations
        self.invoke = invoke

    def handle(self, env_id, token, message):
        grant = self.gate.token_context(env_id, token)
        if grant is None:
            return 401, {'error': 'unauthenticated'}
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            return 400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
        request_id = message.get('id')
        method = message['method']
        params = message.get('params', {})
        if not isinstance(params, dict):
            return 200, self._error(request_id, -32602, 'Invalid params')
        try:
            context = self.resolve_job(env_id, grant['job_id'])
            self.policy.check_job(context['environment'], context['spec'],
                                  required_credentials=context['required_credentials'],
                                  repo_is_public=self.repo_is_public)
            operations = context['operations']
            if not isinstance(operations, list) or not operations:
                raise ValueError('missing operations')
            allowed = {row['name'] for row in operations if isinstance(row, dict) and isinstance(row.get('name'), str)}
            implemented = self.implementations[env_id]
            names = []
            for name in sorted(allowed):
                tool = self.policy.tool_spec(name, path='G')
                if name in implemented and tool['capability'] in grant['capabilities']:
                    names.append(name)
            if method == 'notifications/initialized' and 'id' not in message:
                return 202, None
            if method == 'initialize':
                return 200, self._result(request_id, {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                                                     'serverInfo': {'name': 'ax-sandbox', 'version': '1'}})
            if method == 'tools/list':
                return 200, self._result(request_id, {'tools': [{'name': name, 'inputSchema': {'type': 'object'}} for name in names]})
            if method != 'tools/call':
                return 200, self._error(request_id, -32601, 'Method not found')
            name, args = params.get('name'), params.get('arguments', {})
            if not isinstance(name, str) or not isinstance(args, dict):
                return 200, self._error(request_id, -32602, 'Invalid params')
            if name not in names:
                return 200, self._error(request_id, -32003, 'Operation denied')
            self.policy.authorize_call(name, args, path='G', root=context['root'], job_id=grant['job_id'])
            mapping = self.policy.tool_spec(name, path='G')
            result = self.invoke(env_id, mapping, args)
            encoded = json.dumps(result, ensure_ascii=False).encode('utf-8')
            if not self.policy.scan_inbound(encoded):
                raise ValueError('inbound scan failed')
            return 200, self._result(request_id, {'content': [{'type': 'text', 'text': encoded.decode('utf-8')}]})
        except Exception as exc:
            # Record only stable codes, never request arguments or exception text.
            reason = exc.code if isinstance(exc, BoxError) and exc.code in _DENIAL_CODES else "operation_denied"
            _LOG.warning("sandbox.call path=G result=rejected reason=%s", reason)
            # Never disclose request arguments, credentials, or provider errors.
            return 200, self._error(request_id, -32003, 'Operation denied')

    @staticmethod
    def _result(request_id, result):
        return {'jsonrpc': '2.0', 'id': request_id, 'result': result}

    @staticmethod
    def _error(request_id, code, message):
        return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def build_mcp_routes(service):
    async def dispatch(request: Request):
        authorization = request.headers.get('authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        try:
            message = await request.json()
        except (ValueError, UnicodeError):
            return JSONResponse({'error': 'bad_request'}, status_code=400)
        status, body = await run_in_threadpool(service.handle, request.path_params['env_id'], token, message)
        if body is None:
            return Response(status_code=status)
        return JSONResponse(body, status_code=status, headers={'Cache-Control': 'no-store'})
    return [Route('/mcp/{env_id}', dispatch, methods=['POST'])]
