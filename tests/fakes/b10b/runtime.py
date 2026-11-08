"""Offline HQ signing and in-process ASGI helpers (never a listening socket)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import httpx
from starlette.requests import Request


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# Known Mersenne primes, test-only RSA key. No real credentials or identity.
P, Q, E = (1 << 1279) - 1, (1 << 2203) - 1, 65537
N = P * Q
D = pow(E, -1, (P - 1) * (Q - 1))
JWKS = {"keys": [{"kty": "RSA", "kid": "test-key", "alg": "RS256", "use": "sig",
                  "n": b64(N.to_bytes((N.bit_length() + 7) // 8)), "e": b64(E.to_bytes(3))}]}


def signed(claims=None, *, header=None, body_raw=None):
    header = header or {"alg": "RS256", "kid": "test-key"}
    claims = claims or {"iss": "https://access.ci.invalid", "aud": ["test-aud"], "email": "test-user-3@example.com", "exp": 2000, "nbf": 900}
    head = b64(json.dumps(header, separators=(",", ":")).encode())
    body = b64(body_raw or json.dumps(claims, separators=(",", ":")).encode())
    message = f"{head}.{body}".encode()
    digest = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message).digest()
    width = (N.bit_length() + 7) // 8
    encoded = b"\x00\x01" + b"\xff" * (width - len(digest) - 3) + b"\x00" + digest
    signature = pow(int.from_bytes(encoded), D, N).to_bytes(width)
    return f"{head}.{body}.{b64(signature)}"


def request(headers=(), path="/", method="GET"):
    return Request({"type": "http", "method": method, "path": path, "headers":
                    [(k.lower().encode(), v.encode()) for k, v in headers],
                    "query_string": b"", "server": ("hq.ci.invalid", 443), "scheme": "https"})


def call(app, method="GET", path="/", *, headers=(), data=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hq.ci.invalid") as client:
            return await client.request(method, path, headers=headers, json=data)
    return asyncio.run(run())


class Intake:
    def __init__(self):
        self.calls = []

    def submit(self, destination, payload, **kwargs):
        self.calls.append((destination, payload, kwargs))
        return 200, {"accepted": True}
