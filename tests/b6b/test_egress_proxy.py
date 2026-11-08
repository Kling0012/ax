"""egress-proxy（core/egress-proxy/egress_proxy.py）。許可ファイルの読み方、CONNECT の許可と拒否、ゲートへの素の HTTP の中継、モデルの入口の中継。

偽のストリーム・偽の名前解決・偽の接続（tests/fakes/b6b/streams.py）で動かす。ソケットは使わない。
実際の ax-job → egress-proxy → 外 の疎通、ゲートへの中継の実測（T-182）は実環境（status/b6b.md）。
"""

import ipaddress as _fixture_ipaddress
_NET_FIXTURE_3258 = str(_fixture_ipaddress.ip_network("100.64.0.0/10")[3258])
import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.fakes.b6b.streams import FakeNet, FakeWriter, reader_with

ROOT = Path(__file__).resolve().parents[2]


def load_proxy():
    spec = importlib.util.spec_from_file_location("egress_proxy_under_test", ROOT / "core" / "egress-proxy" / "egress_proxy.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["egress_proxy_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


EP = load_proxy()
GATE = "gate.invalid"  # 試験用の名前（実在のアドレスを埋め込まない）
GW = ("gw.invalid", 3425)


def auth(job):
    return "Proxy-Authorization: Basic " + base64.b64encode(f"job-{job}:".encode()).decode()


def make(tmp_path, net, allow=None):
    d = tmp_path / "egress"
    d.mkdir(exist_ok=True)
    for job, text in (allow or {}).items():
        (d / f"job-{job}.allow").write_text(text)
    logs = []
    ctx = EP.Context(allow_dir=d, gateway=GW, gate_host=GATE, gate_port=8931, resolver=net.resolver, connector=net.connector,
                     log=logs.append, idle_timeout_s=2.0)
    return ctx, logs


def arun(handler, data: bytes, *args, eof=True):
    """StreamReader は動いているループの中で作る必要があるので、読み手をここで作って handler(reader, writer, *args) を動かす。"""
    w = FakeWriter()

    async def go():
        await handler(reader_with(data, eof), w, *args)

    asyncio.run(go())
    return w


def run_proxy(ctx, request: bytes, eof=True):
    return arun(EP.handle_proxy, request, ctx, eof=eof)


def connect_req(host, port, job="j1", extra=""):
    h = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if job:
        h += auth(job) + "\r\n"
    return (h + extra + "\r\n").encode()


def status(w):
    return int(w.data.split(b" ", 2)[1])


def test_read_allow_parses_hosts_gate_and_ignores_garbage(tmp_path):
    d = tmp_path
    (d / "job-a.allow").write_text("pypi.org\n# comment\n\napi.example.org\n@gate 8931\n@other 1\n*.evil.com\n192.0.2.1:443\nEXAMPLE.com\n")
    a = EP.read_allow(d, "a")
    assert a.hosts == frozenset({"pypi.org", "api.example.org", "192.0.2.1"}) or a.hosts == frozenset({"pypi.org", "api.example.org"})
    assert "*.evil.com" not in a.hosts and "example.com" not in a.hosts
    assert a.gate_port == 8931
    assert EP.read_allow(d, "missing") is None
    assert EP.read_allow(d, "../a") is None and EP.read_allow(d, "A") is None and EP.read_allow(d, "") is None


def test_connect_allowed_host_443_is_tunnelled(tmp_path):
    net = FakeNet(dns={"pypi.org": ["1.1.1.1"]}, upstream={("1.1.1.1", 443): b"SERVER-HELLO"})
    ctx, logs = make(tmp_path, net, {"j1": "pypi.org\n"})
    w = run_proxy(ctx, connect_req("pypi.org", 443, extra="") + b"CLIENT-HELLO")
    assert w.data.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n")
    assert w.data.endswith(b"SERVER-HELLO")
    assert net.connected == [("1.1.1.1", 443)]  # 解決したアドレスへ直接つなぐ
    assert net.writers[("1.1.1.1", 443)].data == b"CLIENT-HELLO"
    assert logs == [{"ts": logs[0]["ts"], "event": "connect_allow", "job": "j1", "host": "pypi.org"}]  # ホスト名とジョブ ID だけ


def test_connect_denied_cases_return_403_and_do_not_connect(tmp_path):
    net = FakeNet(dns={"pypi.org": ["1.1.1.1"], "evil.example": ["8.8.8.8"]}, upstream={("1.1.1.1", 443): b"x", ("8.8.8.8", 443): b"x"})
    ctx, logs = make(tmp_path, net, {"j1": "pypi.org\n", "empty": ""})
    cases = [
        connect_req("evil.example", 443),                 # 許可先にない
        connect_req("pypi.org", 80),                      # 443 以外
        connect_req("pypi.org", 8443),
        connect_req("1.1.1.1", 443),                # IP の直指定
        connect_req("[::1]", 443),
        connect_req("pypi.org", 443, job=None),           # 資格なし
        connect_req("pypi.org", 443, job="unknown"),      # ジョブ ID が不明
        connect_req("pypi.org", 443, job="empty"),        # 許可先が空
        b"CONNECT pypi.org:443 HTTP/1.1\r\nProxy-Authorization: Basic !!!notbase64\r\n\r\n",
        b"CONNECT pypi.org:443 HTTP/1.1\r\nProxy-Authorization: Basic " + base64.b64encode(b"root:x") + b"\r\n\r\n",
        b"CONNECT pypi.org:443 HTTP/1.1\r\nProxy-Authorization: Bearer abc\r\n\r\n",
    ]
    for req in cases:
        w = run_proxy(ctx, req)
        assert status(w) == 403, req
        assert b"Connection established" not in w.data
    assert net.connected == [] and net.resolved == []  # 解決もしない
    assert all(set(r) <= {"ts", "event", "job", "host", "reason"} for r in logs)
    assert all(r["event"] == "connect_deny" for r in logs)


def test_connect_host_matching_is_exact_and_case_insensitive(tmp_path):
    net = FakeNet(dns={"pypi.org": ["1.1.1.1"], "files.pypi.org": ["1.1.1.2"]}, upstream={("1.1.1.1", 443): b"ok"})
    ctx, _ = make(tmp_path, net, {"j1": "pypi.org\n"})
    assert status(run_proxy(ctx, connect_req("PyPI.org.", 443))) == 200       # 大文字小文字と末尾のドット
    assert status(run_proxy(ctx, connect_req("files.pypi.org", 443))) == 403  # サブドメインは別（ワイルドカードなし）
    assert status(run_proxy(ctx, connect_req("evilpypi.org", 443))) == 403


@pytest.mark.parametrize("addr", ["198.51.100.10", "127.0.0.1", "169.254.169.254", "203.0.113.133", "172.16.0.1", "__NET_FIXTURE_3258__".replace('__NET_FIXTURE_3258__', _NET_FIXTURE_3258), "::1", "fd00::1", "0.0.0.0", "224.0.0.1", "::ffff:192.0.2.1"])
def test_connect_to_non_public_resolution_is_denied(tmp_path, addr):
    net = FakeNet(dns={"pypi.org": [addr]}, upstream={(addr, 443): b"x"})
    ctx, logs = make(tmp_path, net, {"j1": "pypi.org\n"})
    w = run_proxy(ctx, connect_req("pypi.org", 443))
    assert status(w) == 403 and net.connected == []
    assert logs[-1]["reason"] == "non_public_address"


def test_connect_mixed_resolution_with_private_address_is_denied(tmp_path):
    net = FakeNet(dns={"pypi.org": ["1.1.1.1", "192.0.2.1"]}, upstream={("1.1.1.1", 443): b"x"})
    ctx, _ = make(tmp_path, net, {"j1": "pypi.org\n"})
    assert status(run_proxy(ctx, connect_req("pypi.org", 443))) == 403 and net.connected == []


def test_connect_failures_return_502(tmp_path):
    net = FakeNet(dns={"pypi.org": ["1.1.1.1"]}, upstream={})
    ctx, _ = make(tmp_path, net, {"j1": "pypi.org\nnodns.example\n"})
    assert status(run_proxy(ctx, connect_req("pypi.org", 443))) == 502       # 接続できない
    assert status(run_proxy(ctx, connect_req("nodns.example", 443))) == 502  # 名前解決できない


def test_allow_file_changes_take_effect_per_request_and_other_jobs_are_isolated(tmp_path):
    net = FakeNet(dns={"pypi.org": ["1.1.1.1"], "npm.example": ["8.8.8.8"]},
                  upstream={("1.1.1.1", 443): b"ok", ("8.8.8.8", 443): b"ok"})
    ctx, _ = make(tmp_path, net, {"j1": "pypi.org\n", "j2": "npm.example\n"})
    assert status(run_proxy(ctx, connect_req("pypi.org", 443, job="j1"))) == 200
    assert status(run_proxy(ctx, connect_req("pypi.org", 443, job="j2"))) == 403
    (ctx.allow_dir / "job-j2.allow").write_text("pypi.org\n")
    assert status(run_proxy(ctx, connect_req("pypi.org", 443, job="j2"))) == 200
    (ctx.allow_dir / "job-j1.allow").rename(tmp_path / "moved")  # ファイルが無くなったら全拒否
    assert status(run_proxy(ctx, connect_req("pypi.org", 443, job="j1"))) == 403


def test_malformed_requests(tmp_path):
    net = FakeNet()
    ctx, _ = make(tmp_path, net, {"j1": "pypi.org\n"})
    assert status(run_proxy(ctx, b"GARBAGE\r\n\r\n")) == 400
    assert status(run_proxy(ctx, b"CONNECT nohost HTTP/1.1\r\n" + auth("j1").encode() + b"\r\n\r\n")) == 400
    assert status(run_proxy(ctx, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")) == 400            # プロキシ形式でない
    assert status(run_proxy(ctx, b"CONNECT pypi.org:443 HTTP/1.1\r\nbadheader\r\n\r\n")) == 400
    assert status(run_proxy(ctx, b"A" * 40000, eof=False)) == 400


def plain_req(url, job="j1", method="GET", extra="", body=b""):
    h = f"{method} {url} HTTP/1.1\r\nHost: ignored\r\n"
    if job:
        h += auth(job) + "\r\n"
    return (h + "Proxy-Connection: keep-alive\r\nAccept: */*\r\n" + extra + "\r\n").encode() + body


def test_plain_http_to_gate_allowed_only_with_gate_line(tmp_path):
    net = FakeNet(upstream={(GATE, 8931): b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"})
    ctx, logs = make(tmp_path, net, {"j1": "pypi.org\n@gate 8931\n", "j2": "pypi.org\n"})
    w = run_proxy(ctx, plain_req(f"http://{GATE}:8931/v1/tools/screenshot?x=1", job="j1"))
    assert w.data.endswith(b"hi") and w.data.startswith(b"HTTP/1.1 200 OK")
    sent = net.writers[(GATE, 8931)].data.decode("latin-1")
    assert sent.startswith("GET /v1/tools/screenshot?x=1 HTTP/1.1\r\n")   # 絶対 URI を origin-form に直す
    assert f"Host: {GATE}:8931" in sent and "Connection: close" in sent
    assert "Proxy-" not in sent and "proxy-" not in sent                    # 資格を上流へ渡さない
    assert logs[-1]["event"] == "http_allow" and logs[-1]["job"] == "j1"
    net.connected.clear()
    w = run_proxy(ctx, plain_req(f"http://{GATE}:8931/v1/x", job="j2"))     # ゲートの行が無いジョブは 403（T-127 の境界）
    assert status(w) == 403 and net.connected == []


def test_plain_http_other_destinations_denied(tmp_path):
    net = FakeNet(dns={"pypi.org": ["1.1.1.1"]}, upstream={(GATE, 8931): b"x", ("pypi.org", 80): b"x", (GATE, 9999): b"x"})
    ctx, _ = make(tmp_path, net, {"j1": "pypi.org\n@gate 8931\n"})
    for url in (f"http://{GATE}:9999/", "http://pypi.org/", "http://198.51.100.99:8931/", "http://127.0.0.1:8931/", f"http://{GATE}/", "http://169.254.169.254/latest"):
        assert status(run_proxy(ctx, plain_req(url))) == 403, url
    assert status(run_proxy(ctx, plain_req(f"http://{GATE}:8931/", job=None))) == 403
    assert status(run_proxy(ctx, plain_req(f"https://{GATE}:8931/"))) == 400
    assert net.connected == []


def test_plain_http_post_body_is_forwarded(tmp_path):
    net = FakeNet(upstream={(GATE, 8931): b"HTTP/1.1 204 No Content\r\n\r\n"})
    ctx, _ = make(tmp_path, net, {"j1": "@gate 8931\n"})
    req = plain_req(f"http://{GATE}:8931/v1/call", method="POST", extra="Content-Length: 5\r\nContent-Type: application/json\r\n", body=b"hello")
    w = run_proxy(ctx, req)
    sent = net.writers[(GATE, 8931)].data
    assert sent.startswith(b"POST /v1/call HTTP/1.1\r\n") and sent.endswith(b"\r\n\r\nhello")
    assert b"content-length: 5" in sent.lower() and w.data.startswith(b"HTTP/1.1 204")


def test_gate_not_configured_means_all_plain_http_denied(tmp_path):
    net = FakeNet(upstream={(GATE, 8931): b"x"})
    ctx, _ = make(tmp_path, net, {"j1": "@gate 8931\n"})
    ctx.gate_host = None
    assert status(run_proxy(ctx, plain_req(f"http://{GATE}:8931/"))) == 403 and net.connected == []


def test_gateway_relay_is_always_allowed_and_raw(tmp_path):
    net = FakeNet(upstream={GW: b"MODEL-RESPONSE"})
    ctx, logs = make(tmp_path, net, {})
    w = arun(EP.handle_gateway, b"POST /v1/messages HTTP/1.1\r\n\r\n{}", ctx)
    assert w.data == b"MODEL-RESPONSE"
    assert net.writers[GW].data == b"POST /v1/messages HTTP/1.1\r\n\r\n{}"
    assert net.connected == [GW] and net.resolved == []  # 許可ファイルを見ない（ジョブ ID も要らない）
    assert logs[-1]["event"] == "gateway_relay"
    ctx2, _ = make(tmp_path, FakeNet(upstream={}), {})
    w2 = arun(EP.handle_gateway, b"x", ctx2)  # 上流に届かないときは閉じるだけ
    assert w2.data == b"" and w2.closed


def test_gateway_url_parsing_and_context_from_env(tmp_path):
    assert EP.parse_gateway("http://198.51.100.1:3425") == ("198.51.100.1", 3425)
    assert EP.parse_gateway("https://gw.example/") == ("gw.example", 443)
    with pytest.raises(ValueError):
        EP.parse_gateway("ftp://x")
    ctx = EP.context_from_env({"EGRESS_MODEL_GATEWAY": "http://gw.invalid:3425", "EGRESS_ALLOW_DIR": str(tmp_path), "EGRESS_GATE_HOST": "g.invalid", "EGRESS_GATE_PORT": "8931"})
    assert ctx.gateway == ("gw.invalid", 3425) and ctx.gate_host == "g.invalid" and ctx.gate_port == 8931 and ctx.allow_dir == tmp_path
    with pytest.raises(SystemExit):
        EP.context_from_env({})


def test_health_endpoint(tmp_path):
    w = arun(EP.handle_health, b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
    body = json.loads(w.data.split(b"\r\n\r\n", 1)[1])
    assert w.data.startswith(b"HTTP/1.1 200") and body == {"ok": True, "component": "egress-proxy", "version": EP.VERSION}
    w = arun(EP.handle_health, b"GET /other HTTP/1.1\r\n\r\n")
    assert w.data.startswith(b"HTTP/1.1 404")


def test_no_secrets_or_urls_in_log_records(tmp_path):
    net = FakeNet(dns={"pypi.org": ["1.1.1.1"]}, upstream={("1.1.1.1", 443): b"ok"})
    ctx, logs = make(tmp_path, net, {"j1": "pypi.org\n"})
    run_proxy(ctx, connect_req("pypi.org", 443, extra="X-Secret: topsecretvalue\r\n"))
    text = json.dumps(logs)
    assert "topsecretvalue" not in text and "Basic" not in text and "cGFzcw" not in text
