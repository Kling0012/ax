"""S-22.7/T-59 authentication, offline with signed test JWTs."""
from __future__ import annotations

import ipaddress as _fixture_ipaddress
_NET_FIXTURE_3258 = str(_fixture_ipaddress.ip_network("100.64.0.0/10")[3258])
_NET_FIXTURE_3259 = str(_fixture_ipaddress.ip_network("100.64.0.0/10")[3259])

import copy

import pytest

from core.hq.auth import AccessVerifier, Authenticator, AuthFailure, fetch_jwks
from tests.fakes.b10b.runtime import JWKS, request, signed


DIRECT = [("x-forwarded-for", "__NET_FIXTURE_3258__".replace('__NET_FIXTURE_3258__', _NET_FIXTURE_3258)), ("tailscale-user-login", "test-user-3@example.com")]
PUBLIC = [("x-forwarded-for", "192.0.2.1")]


def verifier(**kwargs):
    return AccessVerifier("https://access.ci.invalid", lambda: "test-aud", lambda: JWKS, clock=lambda: 1000, **kwargs)


def auth(**kwargs):
    return Authenticator("192.0.2.1", lambda: {"test-user-3@example.com"}, verifier(), **kwargs)


def test_direct_identity_preserves_b10a_contract():
    who = auth()(request(DIRECT))
    assert who["principal"] == "test-user-3@example.com"
    assert who["direct_tailnet"] and who["via"] == "tailnet"


@pytest.mark.parametrize("headers", [[], DIRECT + [("x-forwarded-for", "__NET_FIXTURE_3259__".replace('__NET_FIXTURE_3259__', _NET_FIXTURE_3259))],
    [("x-forwarded-for", "__NET_FIXTURE_3258__,192.0.2.1".replace('__NET_FIXTURE_3258__', _NET_FIXTURE_3258))], [("x-forwarded-for", "garbage")],
    [("x-forwarded-for", "127.0.0.1")], [("x-forwarded-for", "203.0.113.9")],
    [("x-forwarded-for", "::1")], [("x-forwarded-for", "fd7a:115c:a1e0::1%eth0")],
    [("x-forwarded-for", "__NET_FIXTURE_3258__".replace('__NET_FIXTURE_3258__', _NET_FIXTURE_3258)), ("tailscale-user-login", "test-user-4@example.com")],
    DIRECT + [("tailscale-user-login", "test-user-4@example.com")]])
def test_unknown_missing_multiple_or_unapproved_source_denied(headers):
    assert auth()(request(headers)) is None


def test_tailnet_ipv6_is_allowed():
    assert auth()(request([("x-forwarded-for", "fd7a:115c:a1e0::1"), DIRECT[1]]))


def test_public_never_trusts_tailnet_login_or_host():
    assert auth()(request(PUBLIC + [DIRECT[1], ("host", "hq.ci.invalid")])) is None
    who = auth()(request(PUBLIC + [("cf-access-jwt-assertion", signed()), ("host", "unrelated.ci.invalid")]))
    assert who["via"] == "public" and not who["direct_tailnet"]


@pytest.mark.parametrize("patch", [{"aud": ["wrong"]}, {"iss": "https://wrong.ci.invalid"},
    {"exp": 1000}, {"nbf": 1001}, {"exp": "2000"}, {"exp": True},
    {"exp": float("inf")}, {"nbf": None}, {"aud": ""}, {"email": None}])
def test_bad_claims_rejected(patch):
    claims = {"iss": "https://access.ci.invalid", "aud": ["test-aud"], "email": "test-user-3@example.com", "exp": 2000, "nbf": 900}
    claims.update(patch)
    with pytest.raises(AuthFailure, match="jwt_invalid"):
        verifier().verify(signed(claims))


@pytest.mark.parametrize("header", [{"alg": "none", "kid": "test-key"}, {"alg": "HS256", "kid": "test-key"},
    {"alg": "RS256", "kid": "missing"}, {"alg": "RS256", "kid": "test-key", "crit": ["unknown"]}])
def test_algorithms_unknown_keys_and_critical_headers_denied(header):
    with pytest.raises(AuthFailure):
        verifier().verify(signed(header=header))


def test_signature_tampering_duplicate_claims_and_duplicate_keys_rejected():
    token = signed()
    head, body, sig = token.split(".")
    with pytest.raises(AuthFailure):
        verifier().verify(f"{head}.{body}.{'A' if sig[0] != 'A' else 'B'}{sig[1:]}")
    with pytest.raises(AuthFailure):
        verifier().verify(signed(body_raw=b'{"aud":"test-aud","aud":"bad"}'))
    v = verifier()
    v.jwks = lambda: {"keys": JWKS["keys"] * 2}
    with pytest.raises(AuthFailure):
        v.verify(token)


def test_jwks_fetch_failure_does_not_trust_stale_key():
    v = verifier()
    token = signed()
    assert v.verify(token)["email"] == "test-user-3@example.com"
    def failed():
        raise OSError("private diagnostic not for response")
    v.jwks = failed
    with pytest.raises(AuthFailure, match="jwt_invalid"):
        v.verify(token)


def test_jwks_fetch_uses_https_without_redirect_or_jwt_urls(monkeypatch):
    with pytest.raises(AuthFailure, match="jwks_unavailable"):
        fetch_jwks("http://not-permitted.ci.invalid/keys")
    calls = []
    class Reply:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            calls.append(limit)
            return b'{"keys":[]}'
    class Opener:
        def open(self, req, timeout):
            calls.append((req.full_url, timeout))
            return Reply()
    monkeypatch.setattr("core.hq.auth.build_opener", lambda handler: Opener())
    assert fetch_jwks("https://keys.ci.invalid/certs") == {"keys": []}
    assert calls == [("https://keys.ci.invalid/certs", 5), 262145]


def test_public_allowlist_union_and_missing_email_card_contains_slot_only():
    cards = []
    claims = {"iss": "https://access.ci.invalid", "aud": "test-aud", "email": "test-user-5@example.com", "exp": 2000, "nbf": 900}
    headers = PUBLIC + [("cf-access-jwt-assertion", signed(claims))]
    assert auth(public_users=lambda: {"test-user-5@example.com"})(request(headers))
    # Public-only names must not authorize the direct route.
    assert auth(public_users=lambda: {"test-user-5@example.com"})(request([DIRECT[0], ("tailscale-user-login", "test-user-5@example.com")])) is None
    checker = auth(missing_email=lambda: cards.append({"slot": "hq-access-email"}))
    req = request(headers)
    assert checker(req) is None and req.state.auth_reason == "email_not_allowed"
    assert cards == [{"slot": "hq-access-email"}]


def test_bad_rsa_key_parameters_denied():
    for key_patch in ({"kty": "EC"}, {"use": "enc"}, {"alg": "RS512"}, {"key_ops": ["sign"]}, {"n": "AQ"}, {"e": "Ag"}):
        keys = copy.deepcopy(JWKS)
        keys["keys"][0].update(key_patch)
        v = verifier()
        v.jwks = lambda: keys
        with pytest.raises(AuthFailure):
            v.verify(signed())
