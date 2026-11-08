"""axcommon/chat_json.py（chat-json の runner のクライアント）。偽の Transport で動かす。ネットワークは使わない。"""
import json

import pytest

from axcommon import chat_json as cj
from axcommon.client import Unavailable

BASE = "http://egress-proxy:3425"
TOKEN = "tok-dummy-chatjson-0123456789"  # ダミー


class Rec:
    def __init__(self, status=200, body=None, exc=None):
        self.status, self.body, self.exc, self.calls = status, body, exc, []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, headers, json.loads(data) if data else None, timeout))
        if self.exc:
            raise self.exc
        b = self.body if isinstance(self.body, (bytes, bytearray)) else json.dumps(self.body).encode()
        return self.status, {}, b


def reply(text, **extra):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "m/x", "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 11, "output_tokens": 7}, **extra}


def setup(tmp_path, req=None):
    (tmp_path / "request.json").write_text(json.dumps(req or {"model": "m/x", "max_tokens": 100, "system": "s", "messages": [{"role": "user", "content": "hi"}]}))
    return {"ANTHROPIC_BASE_URL": BASE, "ANTHROPIC_AUTH_TOKEN": TOKEN}


def out(tmp_path):
    return json.loads((tmp_path / "response.json").read_text())


def test_success_writes_text_json_and_usage(tmp_path):
    env = setup(tmp_path)
    t = Rec(body=reply('{"verdict": "ok", "n": 3}'))
    assert cj.run(tmp_path, env, t) == 0
    o = out(tmp_path)
    assert o["ok"] is True and o["exit_code"] == 0 and o["json"] == {"verdict": "ok", "n": 3}
    assert o["usage"] == {"input_tokens": 11, "output_tokens": 7} and o["model"] == "m/x" and o["stop_reason"] == "end_turn"
    method, url, headers, body, timeout = t.calls[0]
    assert method == "POST" and url == BASE + "/v1/messages"
    assert body == {"model": "m/x", "max_tokens": 100, "system": "s", "messages": [{"role": "user", "content": "hi"}]}  # schema は送らない
    assert headers["authorization"] == "Bearer " + TOKEN and headers["anthropic-version"]
    assert TOKEN not in json.dumps(out(tmp_path))  # 出力に鍵を残さない


def test_fenced_json_is_accepted(tmp_path):
    env = setup(tmp_path)
    assert cj.run(tmp_path, env, Rec(body=reply('```json\n{"a": 1}\n```'))) == 0
    assert out(tmp_path)["json"] == {"a": 1}


def test_not_json_is_exit_4(tmp_path):
    env = setup(tmp_path)
    assert cj.run(tmp_path, env, Rec(body=reply("I think it is fine."))) == 4
    o = out(tmp_path)
    assert o["ok"] is False and o["error"]["class"] == "not_json" and o["text"] == "I think it is fine."


def test_schema_mismatch_is_exit_5_with_errors(tmp_path):
    schema = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"enum": ["ok", "ng"]}}, "additionalProperties": False}
    env = setup(tmp_path, {"model": "m/x", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}], "schema": schema})
    assert cj.run(tmp_path, env, Rec(body=reply('{"verdict": "maybe"}'))) == 5
    o = out(tmp_path)
    assert o["error"]["class"] == "schema_mismatch" and o["error"]["schema_errors"] and o["json"] == {"verdict": "maybe"}
    assert cj.run(tmp_path, env, Rec(body=reply('{"verdict": "ng"}'))) == 0


@pytest.mark.parametrize("status,body,cls", [
    (429, {"error": {"message": "rate limit"}}, "rate_limited"),
    (429, {"error": {"message": "You exceeded your current quota"}}, "quota"),
    (401, {"error": "no"}, "unauthorized"),
    (403, {"error": "no"}, "unauthorized"),
    (500, {"error": "boom"}, "server_error"),
    (503, {}, "server_error"),
    (400, {"error": "bad"}, "gateway"),
])
def test_http_errors_are_exit_3_with_class(tmp_path, status, body, cls):
    env = setup(tmp_path)
    assert cj.run(tmp_path, env, Rec(status=status, body=body)) == 3
    o = out(tmp_path)
    assert o["ok"] is False and o["error"]["class"] == cls and o["error"]["status"] == status


def test_unavailable_and_garbage_responses_are_exit_3(tmp_path):
    env = setup(tmp_path)
    assert cj.run(tmp_path, env, Rec(exc=Unavailable("ConnectionError"))) == 3
    assert out(tmp_path)["error"]["class"] == "unavailable"
    assert cj.run(tmp_path, env, Rec(body=b"<html>")) == 3
    assert cj.run(tmp_path, env, Rec(body=b"[1,2]")) == 3


def test_bad_request_file_is_exit_2_and_no_call(tmp_path):
    t = Rec(body=reply("{}"))
    env = {"ANTHROPIC_BASE_URL": BASE, "ANTHROPIC_AUTH_TOKEN": TOKEN}
    assert cj.run(tmp_path, env, t) == 2  # request.json が無い
    for bad in ({"model": "m"}, {"model": "m", "max_tokens": 0, "messages": [{"role": "user", "content": "x"}]},
                {"model": "m", "max_tokens": 5, "messages": []}, {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "x"}], "stream": True},
                [1]):
        (tmp_path / "request.json").write_text(json.dumps(bad))
        assert cj.run(tmp_path, env, t) == 2, bad
    (tmp_path / "request.json").write_text("not json")
    assert cj.run(tmp_path, env, t) == 2
    assert t.calls == []


def test_missing_env_is_exit_3_and_no_call(tmp_path):
    setup(tmp_path)
    t = Rec(body=reply("{}"))
    assert cj.run(tmp_path, {}, t) == 3
    assert cj.run(tmp_path, {"ANTHROPIC_BASE_URL": BASE}, t) == 3
    assert cj.run(tmp_path, {"ANTHROPIC_BASE_URL": "ftp://x", "ANTHROPIC_AUTH_TOKEN": TOKEN}, t) == 3
    assert t.calls == [] and out(tmp_path)["error"]["class"] == "gateway"


def test_reads_only_the_two_env_vars_not_the_profile_gateway(tmp_path):
    env = setup(tmp_path)
    env["AX_MODEL_GATEWAY"] = "http://other.invalid:3425"
    t = Rec(body=reply("{}"))
    assert cj.run(tmp_path, env, t) == 0
    assert t.calls[0][1].startswith(BASE)


def test_main_uses_work_dir_env(tmp_path, monkeypatch):
    setup(tmp_path)
    monkeypatch.setenv("AX_CHAT_JSON_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", BASE)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(cj, "urllib_transport", Rec(body=reply('{"x": 1}')))
    assert cj.main([]) == 0
    assert out(tmp_path)["json"] == {"x": 1}


def test_request_schema_file_matches_the_minimal_check(repo_root):
    import jsonschema

    sch = json.loads((repo_root / "schemas/ext/b6b/chat-json-request.schema.json").read_text())
    v = jsonschema.Draft202012Validator(sch)
    for ok in ({"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "x"}]},
               {"model": "m", "max_tokens": 5, "system": "s", "messages": [{"role": "user", "content": "x"}], "schema": {"type": "object"}}):
        assert not list(v.iter_errors(ok)) and cj._check_request(ok) == []
    for bad in ({"model": "m"}, {"model": "m", "max_tokens": 0, "messages": [{"role": "user", "content": "x"}]}, {"model": "m", "max_tokens": 5, "messages": [], "x": 1}):
        assert list(v.iter_errors(bad)) and cj._check_request(bad)
