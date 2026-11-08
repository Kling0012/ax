"""Authentication for the change notification receipt endpoints."""
import hmac
import ipaddress
import os

from axcommon import creds


def authorize_component(request, settings):
    if getattr(settings, "sample", False) or request.headers.get("x-hq-view") in {"sample", "fixture"}:
        return 403
    # HQ is served on its existing Unix socket. Requests forwarded from the
    # public proxy or a browser must never enter this component-only API.
    try:
        client = request.scope.get("client")
        network = ipaddress.ip_network(os.environ.get("AX_WORK_SUBNET", "198.51.100.0/25"), strict=False)
        if not client or ipaddress.ip_address(client[0]) not in network:
            return 403
    except (ValueError, TypeError, IndexError):
        return 403
    if any(request.headers.getlist(name) for name in ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip", "origin")):
        return 403
    headers = request.headers.getlist("authorization")
    if len(headers) != 1:
        return 401
    expected = creds.read_secret_optional("plugin-token-change")
    supplied = creds.parse_bearer(headers[0])
    if not expected:
        return 503
    if not supplied or not hmac.compare_digest(supplied, expected):
        return 401
    return None
