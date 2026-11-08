"""偽の部品（tests/fakes/）の振る舞い（B0）。偽のモデルの入口: 決まった応答・429・遅延。"""
import pytest

from axcommon.client import ApiClient, ApiError, ServerError
from axcommon.errors import ErrorRules
from tests.fakes.model_gateway import Delay, FakeModelGateway, Quota, RateLimit, Reply, ServerError as GwServerError, Unauthorized


def _client(gw, key="dummy-key"):
    return ApiClient("http://gw.invalid:3425", token=key, transport=gw)


def _msg(c, model="fake/model-a"):
    return c.post("/v1/messages", {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]})


def test_default_reply_and_scripted_reply_in_order(fake_gateway):
    fake_gateway.script(Reply("一"), Reply("二"))
    c = _client(fake_gateway)
    assert _msg(c)["content"][0]["text"] == "一"
    assert _msg(c)["content"][0]["text"] == "二"
    assert _msg(c)["content"][0]["text"] == "ok"  # script が空なら default


def test_reply_echoes_requested_model_and_records_request(fake_gateway):
    r = _msg(_client(fake_gateway), "fake/model-b")
    assert r["model"] == "fake/model-b"
    assert fake_gateway.requests[0].model == "fake/model-b" and fake_gateway.requests[0].messages[0]["content"] == "hi"


def test_rate_limit_is_429_and_classified_as_rate_limit(fake_gateway):
    fake_gateway.script(RateLimit(retry_after_s=7))
    with pytest.raises(ApiError) as ei:
        _msg(_client(fake_gateway))
    assert ei.value.status == 429
    c = ErrorRules.load().classify(http_status=ei.value.status, body=str(ei.value.body))
    assert (c.cls, c.code) == ("system.expected", "rate_limit")


def test_quota_is_429_with_quota_body_and_classified_as_quota(fake_gateway):
    fake_gateway.script(Quota())
    with pytest.raises(ApiError) as ei:
        _msg(_client(fake_gateway))
    c = ErrorRules.load().classify(http_status=ei.value.status, body=str(ei.value.body))
    assert c.code == "quota"


def test_server_error_is_5xx_unavailable(fake_gateway):
    fake_gateway.script(GwServerError(503))
    with pytest.raises(ServerError):
        _msg(_client(fake_gateway))


def test_delay_advances_fake_clock_without_real_waiting(fake_gateway, fake_clock):
    import time

    fake_gateway.script(Delay(30, Reply("遅い")))
    t0 = time.monotonic()
    r = _msg(_client(fake_gateway))
    assert r["content"][0]["text"] == "遅い"
    assert fake_gateway.delays == [30] and fake_clock.sleeps == [30]
    assert time.monotonic() - t0 < 5


def test_missing_or_wrong_key_is_401_and_counted(fake_gateway):
    with pytest.raises(ApiError) as ei:
        _msg(_client(fake_gateway, key="wrong"))
    assert ei.value.status == 401
    with pytest.raises(ApiError):
        _msg(ApiClient("http://gw.invalid", transport=fake_gateway))
    assert fake_gateway.unauthorized_count == 2


def test_x_api_key_header_is_accepted(fake_gateway):
    status, _h, _b = fake_gateway("GET", "http://gw.invalid/v1/models", {"x-api-key": "dummy-key"}, None, 1)
    assert status == 200


def test_models_list_and_unknown_route(fake_gateway):
    c = _client(fake_gateway)
    assert [m["id"] for m in c.get("/v1/models")["data"]] == ["fake/model-a", "fake/model-b"]
    with pytest.raises(ApiError) as ei:
        c.get("/v1/nothing")
    assert ei.value.status == 404


def test_gateway_with_no_key_requirement_accepts_anything():
    gw = FakeModelGateway(api_key=None)
    assert _msg(ApiClient("http://gw.invalid", transport=gw))["type"] == "message"


def test_fake_transport_records_and_404s_unknown(fake_transport):
    c = ApiClient("http://t:1", transport=fake_transport)
    with pytest.raises(ApiError) as ei:
        c.get("/x")
    assert ei.value.status == 404 and fake_transport.paths("GET") == ["/x"]


def test_fake_clock_installs_into_timeutil_and_uninstalls(fake_clock):
    from axcommon import timeutil

    fake_clock.advance(60)
    assert timeutil.now_ts() == "2026-11-05T12:01:00.000Z"
    fake_clock.uninstall()
    assert timeutil.now_ts() != "2026-11-05T12:01:00.000Z"


def test_asgi_app_serves_same_behaviour():
    pytest.importorskip("starlette.testclient")
    from starlette.testclient import TestClient

    gw = FakeModelGateway(api_key="k")
    gw.script(RateLimit())
    with TestClient(gw.asgi_app()) as c:
        assert c.post("/v1/messages", json={"model": "m", "messages": []}, headers={"authorization": "Bearer k"}).status_code == 429
        assert c.get("/v1/models").status_code == 401
