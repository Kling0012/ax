"""S-22.7: serve source classification and Cloudflare Access verification."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class AuthFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def single_header(request, name: str, *, required=True) -> str | None:
    values = request.headers.getlist(name)
    if len(values) != 1 or not values[0].strip():
        if not values and not required:
            return None
        raise AuthFailure("invalid_" + name.lower().replace("-", "_"))
    return values[0].strip()


def source_address(request, proxy_ip: str) -> tuple[str, bool]:
    try:
        proxy = ipaddress.ip_address(proxy_ip)
        raw = single_header(request, "x-forwarded-for")
        if "," in raw or "%" in raw:
            raise ValueError()
        source = ipaddress.ip_address(raw)
        if source == proxy:
            return str(source), True
        networks = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
        if not any(source.version == net.version and source in net for net in networks):
            raise ValueError()
        return str(source), False
    except (ValueError, TypeError):
        raise AuthFailure("source_invalid") from None


def _decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError()
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _json(raw):
    data = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(data, dict):
        raise ValueError()
    return data


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_jwks(url: str) -> dict:
    """Only a configured HTTPS endpoint; no JWT-provided URL or redirects."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc or parsed.fragment:
        raise AuthFailure("jwks_unavailable")
    try:
        with build_opener(_NoRedirect).open(Request(url, headers={"Accept": "application/json"}), timeout=5) as response:
            data = response.read(262145)
        if len(data) > 262144:
            raise ValueError()
        return _json(data)
    except Exception:
        raise AuthFailure("jwks_unavailable") from None


class AccessVerifier:
    """RSASSA-PKCS1-v1_5/SHA-256 with exact encoded-message comparison.

    Only the public-key operation is needed. No signing or private key is held.
    JWKS is fetched per authentication: retrieval failure never trusts stale keys.
    """
    def __init__(self, issuer: str, audience: Callable[[], str], jwks: Callable[[], dict], *, clock=time.time):
        self.issuer, self.audience, self.jwks, self.clock = issuer, audience, jwks, clock

    def verify(self, token: str) -> dict:
        try:
            if len(token) > 16384:
                raise ValueError()
            head, body, sig = token.split(".")
            header, claims = _json(_decode(head)), _json(_decode(body))
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or header.get("crit"):
                raise ValueError()
            keys = self.jwks().get("keys")
            if not isinstance(keys, list) or len(keys) > 64:
                raise ValueError()
            matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]]
            if len(matches) != 1:
                raise ValueError()
            key = matches[0]
            if key.get("kty") != "RSA" or key.get("alg", "RS256") != "RS256" or key.get("use", "sig") != "sig":
                raise ValueError()
            if "key_ops" in key and "verify" not in key["key_ops"]:
                raise ValueError()
            n, e = int.from_bytes(_decode(key["n"])), int.from_bytes(_decode(key["e"]))
            if not 2048 <= n.bit_length() <= 8192 or e < 3 or e % 2 == 0 or e > 2**32:
                raise ValueError()
            signature = _decode(sig)
            width = (n.bit_length() + 7) // 8
            value = int.from_bytes(signature)
            if len(signature) != width or value >= n:
                raise ValueError()
            digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(f"{head}.{body}".encode("ascii")).digest()
            expected = b"\x00\x01" + b"\xff" * (width - len(digest_info) - 3) + b"\x00" + digest_info
            if not hmac.compare_digest(pow(value, e, n).to_bytes(width), expected):
                raise ValueError()
            audience = self.audience()
            aud = claims.get("aud")
            audiences = [aud] if isinstance(aud, str) else aud
            if not audience or not isinstance(audiences, list) or not all(isinstance(v, str) for v in audiences) or audience not in audiences:
                raise ValueError()
            if not self.issuer or claims.get("iss") != self.issuer:
                raise ValueError()
            now = self.clock()
            for field in ("exp", "nbf"):
                value = claims.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError()
            if claims["exp"] <= now or claims["nbf"] > now or not isinstance(claims.get("email"), str):
                raise ValueError()
            return claims
        except AuthFailure:
            raise
        except Exception:
            raise AuthFailure("jwt_invalid") from None


@dataclass
class Authenticator:
    proxy_ip: str
    allowed_users: Callable[[], set[str]]
    access: AccessVerifier | None = None
    public_users: Callable[[], set[str]] = lambda: set()
    missing_email: Callable[[], None] | None = None

    def authenticate(self, request) -> dict:
        _, public = source_address(request, self.proxy_ip)
        try:
            allowed = self.allowed_users()
            if public:
                token = single_header(request, "cf-access-jwt-assertion")
                if self.access is None:
                    raise AuthFailure("jwt_unavailable")
                principal = self.access.verify(token)["email"]
                if principal not in allowed | self.public_users():
                    if self.missing_email is not None:
                        try:
                            self.missing_email()
                        except Exception:
                            pass  # The app retains the pending slot; never expose email.
                    raise AuthFailure("email_not_allowed")
            else:
                principal = single_header(request, "tailscale-user-login")
                if principal not in allowed:
                    raise AuthFailure("login_not_allowed")
        except AuthFailure:
            raise
        except Exception:
            raise AuthFailure("auth_unavailable") from None
        return {"principal": principal, "email": principal, "via": "public" if public else "tailnet",
                "direct_tailnet": not public, "allowed": True}

    def __call__(self, request) -> dict | None:
        cached = request.scope.get("hq.identity")
        if cached is not None:
            return cached
        try:
            identity = self.authenticate(request)
        except AuthFailure as exc:
            request.state.auth_reason = exc.code
            return None
        request.scope["hq.identity"] = identity
        return identity
