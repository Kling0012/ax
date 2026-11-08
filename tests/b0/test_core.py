"""axcommon の基本部: ULID、時刻、既定値、エラー分類、情報区分、資格、拡張（B0。S-2.1、S-2.3、S-17、S-15.1）。"""
import re
import socket

import pytest

from axcommon import creds, ext, ids, policy, timeutil
from axcommon.defaults import Defaults, DefaultsError
from axcommon.errors import ErrorRules, error_event_data


# ---- ULID（S-2.1）----
def test_ulid_is_26_chars_crockford_and_valid():
    u = ids.new_ulid()
    assert len(u) == 26 and ids.is_ulid(u)
    assert re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", u)


def test_ulid_sorts_by_creation_time_even_within_one_millisecond():
    made = [ids.new_ulid(now_ms=1_700_000_000_000) for _ in range(500)]
    assert made == sorted(made)
    assert len(set(made)) == 500


def test_ulid_time_part_roundtrips():
    assert ids.ulid_time_ms(ids.new_ulid(now_ms=1_700_000_123_456)) == 1_700_000_123_456


@pytest.mark.parametrize("bad", ["", "abc", "01j00000000000000000000000", "81J00000000000000000000000", "01J0000000000000000000000I", None, 5])
def test_is_ulid_rejects_malformed(bad):
    assert not ids.is_ulid(bad)


# ---- 時刻（S-2.1）----
def test_format_ts_is_utc_rfc3339_with_milliseconds(fake_clock):
    assert timeutil.now_ts() == "2026-11-05T12:00:00.000Z"
    fake_clock.advance(1.5)
    assert timeutil.now_ts() == "2026-11-05T12:00:01.500Z"


def test_parse_ts_handles_jst_offset_and_roundtrips():
    dt = timeutil.parse_ts("2026-11-05T21:00:00+09:00")
    assert timeutil.format_ts(dt) == "2026-11-05T12:00:00.000Z"
    assert timeutil.format_ts(timeutil.parse_ts("2026-11-05T12:00:00.123456Z")) == "2026-11-05T12:00:00.123Z"


def test_parse_ts_rejects_non_rfc3339():
    with pytest.raises(ValueError):
        timeutil.parse_ts("2026/11/05 12:00")


def test_add_seconds():
    assert timeutil.add_seconds("2026-11-05T12:00:00.000Z", 3600) == "2026-11-05T13:00:00.000Z"


# ---- 既定値（S-17）----
def test_every_s17_row_is_in_defaults(repo_root):
    spec_path = repo_root / "docs" / "SPEC.md"
    if not spec_path.is_file():
        pytest.skip("design document unavailable")
    spec = spec_path.read_text(encoding="utf-8")
    sec = spec.split("## S-17 ", 1)[1].split("\n## S-18 ", 1)[0]
    names = set(re.findall(r"^\| `([a-z_0-9]+)`", sec, re.M))
    d = Defaults.load()
    assert len(names) >= 160
    assert names <= set(d.names())


def test_defaults_values_from_spec():
    d = Defaults.load()
    assert d.get("heartbeat_s") == 60
    assert d.get("longpoll_s") == 30
    assert d.get("system_retries") == 3
    assert d.get("gate_changed_coverage") == 0.8
    assert d.get("env_use_day_tz") == "Asia/Tokyo"
    assert d.get("morning_digest_time_jst") == "07:00 JST"


def test_every_numeric_default_is_inside_its_own_range():
    d = Defaults.load()
    bad = [n for n in d.names() if d.range(n) and isinstance(d.entry(n)["default"], (int, float)) and not d.in_range(n, d.entry(n)["default"])]
    assert bad == []


def test_override_outside_range_is_rejected_inside_is_accepted():
    d = Defaults.load()
    assert d.get("heartbeat_s", override=15) == 15
    with pytest.raises(DefaultsError):
        d.get("heartbeat_s", override=14)
    with pytest.raises(DefaultsError):
        d.get("heartbeat_s", override=301)
    with pytest.raises(DefaultsError):
        d.get("heartbeat_s", override=True)


def test_unknown_default_name_rejected():
    with pytest.raises(DefaultsError):
        Defaults.load().get("no_such_knob")


def test_retry_wait_is_30s_doubling_capped_at_30_minutes():
    d = Defaults.load()
    assert [d.retry_wait_s(i) for i in range(4)] == [30, 60, 120, 240]
    assert d.retry_wait_s(20) == 1800


# ---- エラーの分類（S-2.3）----
@pytest.fixture
def rules():
    return ErrorRules.load()


def test_http_429_is_rate_limit_not_counted_as_failure(rules):
    c = rules.classify(http_status=429, body="Too Many Requests")
    assert (c.cls, c.code, c.counts_as_failure) == ("system.expected", "rate_limit", "no")


def test_rate_limit_text_without_status_is_rate_limit(rules):
    assert rules.classify(body="Rate limit reached for model").code == "rate_limit"


def test_quota_text_wins_over_429(rules):
    c = rules.classify(http_status=429, body='{"error":{"type":"insufficient_quota"}}')
    assert (c.cls, c.code) == ("system.expected", "quota")


def test_timeout_exceptions(rules):
    assert rules.classify(exception=TimeoutError()).code == "timeout"
    assert rules.classify(exception="ReadTimeout").code == "timeout"


@pytest.mark.parametrize("status", [500, 502, 503, 599])
def test_5xx_is_provider_down(rules, status):
    assert rules.classify(http_status=status).code == "provider_down"


def test_connection_refused_is_provider_down_and_name_resolution_is_network(rules):
    assert rules.classify(exception=ConnectionRefusedError()).code == "provider_down"
    assert rules.classify(exception=socket.gaierror(-2, "Name or service not known")).code == "network"


def test_artifact_bug_codes_come_from_hint_and_count_as_failure(rules):
    for code in ("test_fail", "verify_fail", "gate_fail", "bad_output"):
        c = rules.classify(hint=code)
        assert (c.cls, c.code, c.counts_as_failure) == ("artifact.bug", code, "yes")


def test_unmatched_is_unknown_and_held(rules):
    c = rules.classify(http_status=418, body="teapot")
    assert (c.cls, c.code, c.counts_as_failure) == ("unknown", "unclassified", "hold")


def test_404_is_not_misclassified_as_system_expected(rules):
    assert rules.classify(http_status=404).cls == "unknown"


def test_classify_exception_follows_cause_chain(rules):
    try:
        try:
            raise TimeoutError("slow")
        except TimeoutError as e:
            raise RuntimeError("wrapped") from e
    except RuntimeError as outer:
        assert rules.classify_exception(outer).code == "timeout"


def test_error_event_data_has_short_message_and_valid_shape(rules):
    from axcommon import schemas

    c = rules.classify(http_status=429)
    data = error_event_data(c, "x" * 1000)
    assert len(data["message"]) <= 300
    ev = {"id": ids.new_ulid(), "ts": "2026-11-05T12:00:00.000Z", "source": {"component": "c", "host": "h"}, "kind": "error.classified",
          "subject": "s", "data": data}
    assert schemas.errors("event", ev) == []


def test_retryable_flag(rules):
    assert rules.classify(http_status=429).retryable
    assert not rules.classify(hint="test_fail").retryable
    assert not rules.classify(http_status=503).retryable  # 次の候補へ切り替える（再試行ではない）


# ---- 情報区分（S-15.1、D-14）----
MODELS = {"free": {"id": "m", "tier": "free"}, "flash": {"id": "m", "tier": "flash"}, "std": {"id": "m", "tier": "standard"},
          "last": {"id": "m", "tier": "last_resort"}}
ENVS = {"ssh": {"id": "e", "adapter": "ssh-host", "data_classes": ["public", "private"]},
        "box": {"id": "b", "adapter": "cloud-sandbox", "data_classes": ["public"]},
        "box_wrong": {"id": "b", "adapter": "cloud-sandbox", "data_classes": ["public", "private", "sensitive"]}}


def test_public_goes_to_any_registered_model_and_env():
    assert all(policy.allowed("public", m) for m in MODELS.values())
    assert all(policy.allowed("public", e) for e in ENVS.values())


def test_private_goes_to_registered_models_but_never_to_cloud_sandbox():
    assert policy.allowed("private", MODELS["free"])
    assert policy.allowed("private", ENVS["ssh"])
    assert not policy.allowed("private", ENVS["box"])
    assert not policy.allowed("private", ENVS["box_wrong"])  # 台帳の記載が誤っていても箱には渡さない


def test_sensitive_only_standard_or_higher_models():
    assert not policy.allowed("sensitive", MODELS["free"])
    assert not policy.allowed("sensitive", MODELS["flash"])
    assert policy.allowed("sensitive", MODELS["std"])
    assert policy.allowed("sensitive", MODELS["last"])
    assert not policy.allowed("sensitive", ENVS["ssh"])  # data_classes に sensitive が無い
    assert not policy.allowed("sensitive", ENVS["box_wrong"])


def test_unregistered_target_is_denied_and_bad_class_raises():
    assert not policy.allowed("public", None)
    assert not policy.allowed("public", {})
    with pytest.raises(ValueError):
        policy.allowed("secret", MODELS["std"])


def test_most_restrictive():
    assert policy.most_restrictive("public", "sensitive", "private") == "sensitive"


# ---- 資格（F-18.4）----
def test_read_secret_strips_newline_and_never_leaks_value_in_error(tmp_path):
    (tmp_path / "tok").write_text("abcDEF123\n")
    assert creds.read_secret("tok", base=tmp_path) == "abcDEF123"
    with pytest.raises(creds.SecretMissing) as ei:
        creds.read_secret("nope", base=tmp_path)
    assert "nope" in str(ei.value)


def test_read_secret_rejects_path_traversal(tmp_path):
    for bad in ("../x", "a/b", "", ".."):
        with pytest.raises(creds.SecretMissing):
            creds.read_secret(bad, base=tmp_path)


def test_token_table_identifies_by_hash_and_rejects_unknown(tmp_path):
    tok = "0123456789abcdef" * 4
    f = tmp_path / "core-tokens.sha256"
    f.write_text(f"worker-pool {creds.sha256_hex(tok)}\n# comment\nbadline\nother {'0' * 64}\n")
    t = creds.TokenTable.from_file(f)
    assert t.identify(tok) == "worker-pool"
    assert t.identify(tok + "x") is None
    assert t.identify("") is None and t.identify(None) is None
    assert t.names() == ["other", "worker-pool"]


def test_token_table_keeps_old_entries_when_file_unreadable(tmp_path):
    f = tmp_path / "t"
    f.write_text(f"a {creds.sha256_hex('x')}\n")
    t = creds.TokenTable.from_file(f)
    f.unlink()
    assert t.reload() is False
    assert t.identify("x") == "a"


def test_token_table_repr_hides_hashes():
    assert "0" * 64 not in repr(creds.TokenTable({"a": "0" * 64}))


def test_parse_bearer():
    assert creds.parse_bearer("Bearer abc") == "abc"
    assert creds.parse_bearer("bearer   abc ") == "abc"
    assert creds.parse_bearer("Basic abc") is None
    assert creds.parse_bearer(None) is None
    assert creds.parse_bearer("Bearer") is None


# ---- 拡張（PLAN.json の conventions）----
class _App:
    def __init__(self):
        self.state = type("S", (), {})()
        self.registered = []


def test_extensions_loaded_in_name_order_and_register_called(tmp_path):
    d = tmp_path / "ext"
    d.mkdir()
    (d / "b3.py").write_text("def register(app):\n    app.registered.append('b3')\n")
    (d / "b1.py").write_text("def register(app):\n    app.registered.append('b1')\n")
    (d / "_skip.py").write_text("raise SystemExit(1)\n")
    (d / "__init__.py").write_text("")
    app = _App()
    res = ext.load_extensions(app, d)
    assert app.registered == ["b1", "b3"]
    assert res == {"b1": None, "b3": None}


def test_broken_extension_does_not_stop_others_and_is_reported(tmp_path):
    d = tmp_path / "ext"
    d.mkdir()
    (d / "a.py").write_text("raise ImportError('boom')\n")
    (d / "b.py").write_text("x = 1\n")  # register が無い
    (d / "c.py").write_text("def register(app):\n    app.registered.append('c')\n")
    app = _App()
    res = ext.load_extensions(app, d)
    assert app.registered == ["c"]
    assert res["a"] == "ImportError" and res["b"] == "AttributeError" and res["c"] is None
    assert app.state.ext_failures == ["a", "b"]


def test_missing_or_none_ext_dir_is_noop(tmp_path):
    assert ext.load_extensions(_App(), tmp_path / "none") == {}
    assert ext.load_extensions(_App(), None) == {}


def test_extension_in_directory_with_hyphen_parent_loads(tmp_path):
    d = tmp_path / "memory-connector" / "ext"
    d.mkdir(parents=True)
    (d / "b2.py").write_text("def register(app):\n    app.registered.append('b2')\n")
    app = _App()
    ext.load_extensions(app, d)
    assert app.registered == ["b2"]
