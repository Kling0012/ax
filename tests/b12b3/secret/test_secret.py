from types import SimpleNamespace

import pytest

from core.intake.dest.b12b3.secret import handle


def app_with(push=None):
    return SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(secret_push=push)))


def invoke(app, payload, source="hq"):
    return handle(app, {}, {"payload": payload}, SimpleNamespace(from_=source))


def test_push_passes_only_slot_to_resource_service():
    calls = []
    app = app_with(lambda slot: calls.append(slot) or True)
    assert invoke(app, {"slot": "remote-key"}) == (200, {"delivered": True})
    assert calls == ["remote-key"]


@pytest.mark.parametrize("payload", [{}, {"slot": "../escape"}, {"slot": "ok", "value": "dummy-secret"}, {"slot": 1}])
def test_invalid_payload_rejected_without_echo(payload, caplog):
    calls = []
    status, result = invoke(app_with(lambda slot: calls.append(slot)), payload)
    assert status == 422
    assert not calls
    assert "dummy-secret" not in repr(result) + caplog.text


def test_non_hq_rejected():
    calls = []
    status, _ = invoke(app_with(lambda slot: calls.append(slot)), {"slot": "remote-key"}, "commander")
    assert status == 403
    assert not calls


def test_backend_failure_is_sanitized(caplog):
    def fail(slot):
        raise RuntimeError("dummy-secret")
    status, result = invoke(app_with(fail), {"slot": "remote-key"})
    assert status == 503
    assert "dummy-secret" not in repr(result) + caplog.text


@pytest.mark.parametrize("push", [None, lambda slot: False, lambda slot: {"secret": "dummy-secret"}])
def test_backend_unavailable_or_invalid_result_fails_closed(push, caplog):
    status, result = invoke(app_with(push), {"slot": "remote-key"})
    assert status == 503
    assert "dummy-secret" not in repr(result) + caplog.text
