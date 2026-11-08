from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlencode, urlsplit

from axcommon import schemas

from .identity import Denied
from .contracts import Reply


def result(request_id, value, *, status=200, failed=False):
    return Reply(200, {"jsonrpc": "2.0", "id": request_id, "result": {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "isError": failed or status >= 400}})


def protocol_error(request_id, code, message):
    return Reply(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def input_schema(row, *, write):
    schema = row.get("inputSchema")
    if schema is None and isinstance(row.get("schema"), dict):
        schema = row["schema"]
    if schema is None and write and isinstance(row.get("schema"), str):
        name = row["schema"].removeprefix("schemas/")
        schema = schemas.load(name)
    schema = dict(schema or {"type": "object", "properties": {}})
    if write:
        schema["properties"] = {**schema.get("properties", {}),
                                "request_id": {"type": "string", "minLength": 1},
                                "project": {"type": "string"}, "session": {"type": "string", "maxLength": 64}}
        schema["required"] = list(dict.fromkeys([*schema.get("required", []), "request_id"]))
    return schema


def available(catalog):
    for row in catalog.get("reads", []):
        if row.get("tool"):
            yield row["tool"], row, False
    for row in catalog.get("destinations", []):
        if row.get("mcp_tool"):
            yield row["mcp_tool"], row, True


async def execute(gate, data, evidence, declared, seat, correlation):
    request_id = data.get("id")
    if data.get("jsonrpc") != "2.0" or not isinstance(data.get("method"), str) or (request_id is not None and (isinstance(request_id, (dict, list, bool)))):
        return protocol_error(None, -32600, "Invalid Request")
    method = data["method"]
    if "id" not in data:
        if method == "notifications/initialized":
            return Reply(202, None)
        return protocol_error(None, -32600, "Invalid Request")
    params = data.get("params", {})
    if not isinstance(params, dict):
        return protocol_error(request_id, -32602, "Invalid params")
    try:
        # Every MCP request reaches intake's final state/lock checks, even initialize/tools/list.
        catalog_reply = await gate._relay("GET", "/v1/read/destinations", None,
                                          evidence, declared, correlation, read=True)
        if catalog_reply.status >= 400:
            return result(request_id, catalog_reply.data, status=catalog_reply.status)
        catalog = gate._catalog(catalog_reply.data, seat)
        if not isinstance(catalog, dict) or not isinstance(catalog.get("reads"), list) or not isinstance(catalog.get("destinations"), list):
            return result(request_id, {"error": {"code": "upstream_unavailable", "message": "upstream_unavailable", "hint": ""}}, failed=True)
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2025-03-26"
            return Reply(200, {"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": version, "capabilities": {"tools": {}},
                "serverInfo": {"name": "ax", "version": "0.1.0"}}})
        tools = list(available(catalog))
        if method == "tools/list":
            return Reply(200, {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
                {"name": name, "description": row.get("title", name),
                 "inputSchema": input_schema(row, write=write),
                 **({"annotations": {"readOnlyHint": True}} if not write else {})}
                for name, row, write in tools]}})
        if method != "tools/call":
            return protocol_error(request_id, -32601, "Method not found")
        match = next((item for item in tools if item[0] == params.get("name")), None)
        if match is None:
            return result(request_id, {"error": {"code": "tool_not_found", "message": "tool_not_found", "hint": ""}}, failed=True)
        _, row, write = match
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return protocol_error(request_id, -32602, "Invalid params")
        if write:
            declared_schema = row.get("schema")
            if isinstance(declared_schema, str):
                declared_schema = schemas.load(declared_schema.removeprefix("schemas/"))
            if not isinstance(declared_schema, dict):
                declared_schema = row.get("inputSchema", {})
            payload_fields = declared_schema.get("properties", {}) if isinstance(declared_schema, dict) else {}
            payload = {k: v for k, v in arguments.items()
                       if k != "request_id" and (k not in {"project", "session"} or k in payload_fields)}
            instruction = {"destination": row["name"], "payload": payload,
                           **{k: arguments[k] for k in ("request_id", "project", "session") if k in arguments}}
            instruction = gate._instruction(instruction, seat)
            reply = await gate._relay("POST", "/v1/instructions", instruction, evidence, declared, correlation)
        else:
            if "wait" in arguments or any(isinstance(value, (dict, list)) for value in arguments.values()):
                raise Denied("bad_request", 400)
            route = row.get("path")
            if not isinstance(route, str):
                raise Denied("upstream_unavailable", 503)
            parsed = urlsplit(route)
            # Tool-specific fixed options take precedence (approval verify/export-go).
            query = {**arguments, **dict(parse_qsl(parsed.query, keep_blank_values=True))}
            route = gate._route("GET", parsed.path + ("?" + urlencode(query) if query else ""))
            reply = await gate._relay("GET", route, None, evidence, declared, correlation, read=True)
        unavailable = isinstance(reply.data, dict) and any(isinstance(v, dict) and v.get("reason") == "upstream_unavailable" for v in reply.data.values())
        return result(request_id, reply.data, status=reply.status, failed=unavailable)
    except Denied as exc:
        return result(request_id, {"error": {"code": exc.code, "message": exc.code, "hint": ""}}, status=exc.status)
    except (KeyError, ValueError, schemas.SchemaError):
        return result(request_id, {"error": {"code": "upstream_unavailable", "message": "upstream_unavailable", "hint": ""}}, failed=True)
