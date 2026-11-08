from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from axcommon.ids import new_ulid
from .contracts import RelayEvidence, RelayRequest, Reply, unresolved_transport
from .identity import Denied, Identities
from .limits import Limits

TIERS = {"read": 0, "operate": 1, "irreversible": 2}
READS = {"status", "todos", "events", "brief", "whoami", "destinations", "results", "show", "approvals", "seats", "decisions", "guide"}
ENROLL_CODE = re.compile(r"^[0-9a-f]{32}$")


def error(status, code):
    return Reply(status, {"error": {"code": code, "message": code, "hint": ""}})


def encoded(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Gate:
    """Stateless credential relay; only rate/inflight counters live here."""
    def __init__(self, settings, *, transport=unresolved_transport, registry=None, identities=None, limits=None):
        self.settings, self.transport = settings, transport
        self.identities = identities or Identities(settings)
        self.limits = limits or Limits(settings)
        if registry is None:
            from core.intake.registry import Registry
            registry = Registry.load(settings.config_dir)
        self.registry = registry

    async def dispatch(self, method, path, headers, source_ip, body=b""):
        request_id = new_ulid()
        try:
            header_pairs = [(k.lower(), v) for k, v in headers]
            if sum(k == "host" for k, _ in header_pairs) != 1:
                raise Denied("host_not_allowed", 421)
            h = dict(header_pairs)
            if h["host"].lower() not in self.settings.allowed_hosts:
                raise Denied("host_not_allowed", 421)
            if "origin" in h:
                raise Denied("origin_not_allowed", 403)
            if method not in {"GET", "POST"}:
                raise Denied("method_not_allowed", 405)
            if path == "/healthz" and method == "GET":
                reply = Reply(200, {"ok": True})
            else:
                if len(body) > self.settings.gate_body_max_kb * 1024:
                    raise Denied("too_large", 413)
                peer = self.identities.peer(source_ip)
                if sum(k == "authorization" for k, _ in header_pairs) > 1:
                    raise Denied("unauthorized", 401)
                data = None
                if method == "POST":
                    try:
                        data = json.loads(body)
                        if not isinstance(data, dict):
                            raise ValueError("object required")
                    except (ValueError, UnicodeError):
                        raise Denied("bad_json", 400) from None
                # Forward only these self-declared claims; never forwarded identity/authority headers.
                declared = {name: h["x-ax-" + name][:64] for name in ("agent", "model", "session") if "x-ax-" + name in h}
                bearer = h.get("authorization", "")
                try:
                    core_token = self.settings.core_token_file.read_text().strip()
                except OSError:
                    raise Denied("upstream_unavailable", 503) from None
                evidence = RelayEvidence(core_token, peer["device_id"], peer["device"], bearer[7:] if bearer.startswith("Bearer ") else "")
                if method == "POST" and path == "/v1/enroll":
                    enrollment = RelayEvidence(core_token, peer["device_id"], peer["device"])
                    reply = await self._enroll(data, enrollment, declared, request_id)
                else:
                    seat = self.identities.seat(bearer, peer)
                    reply = await self._authenticated(method, path, data, evidence, declared, seat, request_id)
        except Denied as exc:
            reply = error(exc.status, exc.code)
            if hasattr(exc, "retry_after"):
                reply.headers["Retry-After"] = exc.retry_after
        except Exception:
            # Do not expose request contents, credentials, filesystem paths or exception text.
            reply = error(503, "upstream_unavailable")
        reply.headers["X-AX-Request-Id"] = request_id
        return self._bounded(reply, path)

    async def _relay(self, method, path, data, evidence, declared, request_id, *, read=False):
        timeout = self.settings.gate_call_timeout_s
        if read and not (urlsplit(path).path.endswith("/approvals") and "wait_s=" in path):
            timeout = min(timeout, self.settings.read_relay_timeout_s)
        request = RelayRequest(method, path, evidence, declared, data, request_id)
        try:
            reply = await asyncio.wait_for(self.transport(request, timeout), timeout=timeout)
            if not isinstance(reply, Reply):
                raise ValueError("invalid relay reply")
            # Enrollment is the only endpoint allowed to return credentials once.
            if not path.startswith("/v1/seat/enroll"):
                reply.data = self._without_credentials(reply.data)
            reply.headers = {k: v for k, v in reply.headers.items() if k.lower() == "retry-after"}
            return reply
        except (OSError, TimeoutError, NotImplementedError, ValueError, EOFError):
            if read:
                return Reply(200, {"intake": {"ok": False, "reason": "upstream_unavailable"}})
            return error(503, "upstream_unavailable")

    async def _enroll(self, data, evidence, declared, request_id):
        if not isinstance(data, dict) or set(data) != {"code"} or not isinstance(data.get("code"), str) or not ENROLL_CODE.fullmatch(data["code"]):
            raise Denied("bad_request", 400)
        device = evidence.device_id
        self.limits.enroll_check(device)
        principal = "enroll:" + device
        self.limits.enter(principal, write=True)
        try:
            reply = await self._relay("POST", "/v1/seat/enroll", data, evidence, declared, request_id)
            if reply.status < 500:
                self.limits.enroll_result(device, 200 <= reply.status < 300)
            return reply
        finally:
            self.limits.leave(principal)

    def _route(self, method, path):
        parsed = urlsplit(path)
        route = {"/v1/whoami": "/v1/read/whoami", "/v1/destinations": "/v1/read/destinations"}.get(parsed.path, parsed.path)
        if method == "GET" and route.startswith("/v1/read/") and route[9:] in READS:
            args = parse_qs(parsed.query, keep_blank_values=True)
            if "wait" in args:
                raise Denied("bad_request", 400)
            if "wait_s" in args:
                if route != "/v1/read/approvals" or not args.get("id", [""])[0]:
                    raise Denied("bad_request", 400)
                try:
                    wait = float(args["wait_s"][0])
                    if not __import__("math").isfinite(wait) or wait < 0:
                        raise ValueError("invalid wait")
                except ValueError:
                    raise Denied("bad_request", 400) from None
                args["wait_s"] = [str(min(wait, self.settings.approval_wait))]
            query = urlencode(args, doseq=True)
            return route + ("?" + query if query else "")
        if method == "POST" and route == "/v1/instructions":
            return route
        raise Denied("not_found", 404)

    async def _authenticated(self, method, path, data, evidence, declared, seat, request_id):
        whoami = await self._relay("GET", "/v1/read/whoami", None, evidence, declared, request_id, read=True)
        if whoami.status >= 400:
            return whoami
        is_mcp = method == "POST" and path in {"/mcp", "/mcp/"}
        if is_mcp:
            write = data.get("method") == "tools/call" and not self._is_read_tool(data)
        else:
            self._route(method, path)
            write = method == "POST"
        self.limits.enter(seat["seat_id"], write=write)
        try:
            async with asyncio.timeout(self.settings.gate_call_timeout_s):
                if is_mcp:
                    return await self._mcp(data, evidence, declared, seat, request_id)
                route = self._route(method, path)
                if write:
                    data = self._instruction(data, seat)
                reply = await self._relay(method, route, data, evidence, declared, request_id, read=not write)
                if route.startswith("/v1/read/destinations") and reply.status == 200:
                    reply.data = self._catalog(reply.data, seat)
                return reply
        except TimeoutError:
            return error(503, "upstream_unavailable")
        finally:
            self.limits.leave(seat["seat_id"])

    def _instruction(self, data, seat):
        if not isinstance(data.get("request_id"), str) or not data["request_id"]:
            raise Denied("request_id_required", 400)
        if any(key in data for key in ("from", "principal", "via", "issued_by", "approved_via", "approval_id")) or data.get("confirm_irreversible") not in (None, False):
            raise Denied("forged_authority", 403)
        self.registry.maybe_reload()
        destination = self.registry.get(data.get("destination"))
        if destination is None:
            raise Denied("destination_unknown", 404)
        self._destination(destination, seat)
        result = dict(data)
        if "idempotency_key" in result and result["idempotency_key"] != result["request_id"]:
            raise Denied("idempotency_conflict", 409)
        result["idempotency_key"] = result.pop("request_id")
        result["confirm_irreversible"] = False
        return result

    def _destination(self, destination, seat):
        if "tailnet" not in destination.get("via", []) or "commander" not in destination.get("allowed_from", []):
            raise Denied("receptacle_not_allowed", 403)
        if TIERS.get(destination.get("tier"), 99) > TIERS.get(seat.get("tier"), -1):
            raise Denied("tier_insufficient", 403)

    def _catalog(self, data, seat):
        if not isinstance(data, dict) or "destinations" not in data or "reads" not in data:
            return data
        result = dict(data)
        result["destinations"] = []
        for row in data["destinations"]:
            try:
                # Consult local registry for via/from; do not treat returned metadata as authority.
                destination = self.registry.get(row.get("name"))
                if destination is None:
                    continue
                self._destination(destination, seat)
                result["destinations"].append(row)
            except Denied:
                continue
        result["reads"] = [row for row in data["reads"] if TIERS.get(row.get("tier"), 99) <= TIERS[seat["tier"]]]
        return result

    @staticmethod
    def _without_credentials(value):
        if isinstance(value, dict):
            return {k: Gate._without_credentials(v) for k, v in value.items() if k not in {"token", "token_digest", "public_key", "key_fp", "enroll_code", "code_digest", "bearer", "core_token"}}
        if isinstance(value, list):
            return [Gate._without_credentials(v) for v in value]
        return value

    def _bounded(self, reply, path):
        maximum = self.settings.ax_output_max_kib * 1024
        if len(encoded(reply.data)) <= maximum:
            return reply
        # No partial JSON or in-memory response cache. Resume by issuing the read with a smaller limit.
        data = {"truncated": True, "next": {"path": urlsplit(path).path, "hint": "retry with a smaller limit"}, "data": []}
        source = reply.data if isinstance(reply.data, list) else reply.data.get("data", []) if isinstance(reply.data, dict) else []
        if isinstance(source, list):
            for row in source:
                data["data"].append(row)
                if len(encoded(data)) > maximum:
                    data["data"].pop()
                    break
        if isinstance(reply.data, dict) and reply.data.get("jsonrpc") == "2.0":
            data = {"jsonrpc": "2.0", "id": reply.data.get("id"), "result": {"content": [{"type": "text", "text": encoded({"truncated": True, "next": {"path": "/mcp", "hint": "retry with a smaller limit"}}).decode()}], "isError": False}}
        reply.data = data
        return reply

    @staticmethod
    def _is_read_tool(data):
        # S-6.15 の固定の読み取り名。通常は b12b3 の ops.py を正とする。
        fallback = {"ax_whoami", "ax_destinations", "ax_status", "ax_todos", "ax_events", "ax_brief", "ax_guide", "ax_show",
                    "ax_approvals", "ax_approval_show", "ax_approval_wait", "ax_approval_verify", "ax_approval_export_go",
                    "ax_seats", "ax_decisions"}
        name = data.get("params", {}).get("name")
        try:
            from axcommon.ops import READ_OPS
        except ImportError:
            return name in fallback
        return any(row.get("tool") == name for row in READ_OPS)

    async def _mcp(self, data, evidence, declared, seat, request_id):
        from .mcp import execute
        return await execute(self, data, evidence, declared, seat, request_id)
