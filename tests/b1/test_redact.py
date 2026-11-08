"""T-1 の秘密形式（S-15.3、F-18.6 の索引の形）。入力は実行時に連結して作る（試験のソースに秘密の形の実値を書かない）。"""
import pytest

from axcommon import redact_index
from axcommon.redact import DEFAULT_ALLOW_EMAIL_DOMAINS, REDACTED, Redactor, summarize

A48 = "A1b2C3d4" * 6          # 英数 48 文字
A36 = "Zy9" * 12              # 英数 36 文字
SEG = "abcDEF1234_-xyz"       # JWT の 1 部分（10 文字以上）


def fx():
    return Redactor()


SECRET_INPUTS = {
    "sk": "key is " + "sk" + "-" + A48 + " ok",
    "ghp": "x " + "gh" + "p_" + A36 + " y",
    "akia": "id " + "AK" + "IA" + "ABCDEFGH12345678" + " z",
    "jwt": "t " + ".".join(["ey" + "J" + SEG, SEG, SEG]) + " u",
    "password": "pass" + "word" + "=" + "hunter2hunter",
    "pem": "-----BEGIN " + "PRIVATE" + " KEY-----\nMIIBVgIBADANBgkqhkiG9w0BAQEFAASC\n-----END " + "PRIVATE" + " KEY-----",
    "phone": "tel 090-1234-5678 end",
    "email": "mail test-user@example.org end",
    "bearer": "Authorization: Bearer " + "abcdEFGH1234567890zzzz",
    "github_pat": "github" + "_pat_" + "a1B2c3D4e5" * 4 + "xx",
    "slack": "xox" + "b-" + "1234567890-abcdef",
    "url": "https://user:" + "s3cretPw99" + "@host.invalid/path",
    "tk": "tk" + "_" + "abcdefghij0123456789",
    "ibk": "ib" + "k_" + "ABCDEFGH12345678",
    "axg": "ax" + "g-" + "0123456789abcdef" * 4,
}


@pytest.mark.parametrize("name", sorted(SECRET_INPUTS))
def test_secret_forms_become_redacted(name):
    text = SECRET_INPUTS[name]
    r = (Redactor(allow_email_domains=[]) if name == "email" else fx()).redact_text(text)
    assert REDACTED in r.text and r.hits, name
    # 値そのものが残らない
    probe = {"sk": A48, "ghp": A36, "akia": "ABCDEFGH12345678", "password": "hunter2hunter", "phone": "1234-5678",
             "email": "example.org", "bearer": "abcdEFGH1234567890zzzz", "url": "s3cretPw99"}.get(name)
    if probe:
        assert probe not in r.text


def test_pem_is_replaced_as_a_whole_body():
    text = SECRET_INPUTS["pem"]
    r = fx().redact_text("before\n" + text + "\nafter")
    assert r.text == "before\n" + REDACTED + "\nafter"


def test_pem_without_end_is_cut_to_the_end_of_text():
    r = fx().redact_text("a -----BEGIN RSA " + "PRIVATE KEY-----\nMIIE...")
    assert r.text == "a " + REDACTED


@pytest.mark.parametrize("text", [
    "x@example.net",
    "Co-Authored-By: Someone <test-user@example.org>",
    "commit " + "a1b2c3d4e5" * 4,
    "id 123e4567-e89b-12d3-a456-426614174000",
    "ulid 01J8Z3Q4K5M6N7P8R9S0T1V2W3",
    f"fixture@{DEFAULT_ALLOW_EMAIL_DOMAINS[4]}",
    "x@Example.COM",
    "template pass" + "word" + "=<changeme-later>",
    "pass" + "word" + "=${DB_PASS}",
    "token: ***masked***",
    "api_key=" + "[REDACTED]",
    "secret = example-value-here",
    "pass" + "word: " + "xxxxxxxxxx",
    "2026-11-05T12:00:00.000Z",
    "max_tokens: 100000",
])
def test_non_secrets_stay(text):
    r = fx().redact_text(text)
    assert r.text == text and r.hits == [], summarize(r.hits)


def test_idempotent():
    rd = fx()
    once = rd.redact_text(SECRET_INPUTS["sk"]).text
    assert rd.redact_text(once).text == once


def test_assignment_keeps_key_word_and_replaces_value_only():
    r = fx().redact_text('{"api_key": "' + "abcdefgh" + "12345" + '"}')
    assert r.text == '{"api_key": "' + REDACTED + '"}'


def test_long_hex_or_alnum_is_secret_only_after_assignment_word():
    h = "0123456789abcdef" * 2
    rd = fx()
    assert rd.redact_text("sha " + h).text == "sha " + h
    assert rd.redact_text("token " + h).text == "token " + REDACTED
    alnum = "AbCd1234" * 5
    assert rd.redact_text("name " + alnum).text == "name " + alnum
    assert rd.redact_text("secret " + alnum).text == "secret " + REDACTED


def test_pii_forms():
    rd = fx()
    for text in ("連絡は +81 90-1234-5678 まで", "tel 09012345678", "〒100-0001", "100-0001 東京都", "住所 東京都架空区見本町1丁目9番地"):
        r = rd.redact_text(text)
        assert REDACTED in r.text, text
    assert rd.redact_text("v1.2.3 build 0.5-12-345").hits == []  # 版番号


def test_registered_list_matches_normalized_substrings():
    rd = Redactor(pii_list=["山田　太郎", "Taro.Yamada"])
    r = rd.redact_text("担当は山田 太郎さん、署名 TARO.YAMADA")
    assert "山田" not in r.text and "YAMADA" not in r.text
    assert "repr" not in repr(rd) and "山田" not in repr(rd)


def test_registered_ascii_word_boundaries():
    rd = Redactor(pii_list=["abc", "山田"])
    assert rd.redact_text("abc ABC ａｂｃ (abc) 日本abc語").text == (
        f"{REDACTED} {REDACTED} {REDACTED} ({REDACTED}) 日本{REDACTED}語"
    )
    assert rd.redact_text("abcdef xabc 1abc abc2").text == "abcdef xabc 1abc abc2"
    assert rd.redact_text("担当は山田さん").text == f"担当は{REDACTED}さん"


def test_registered_list_from_file(tmp_path):
    p = tmp_path / "pii-list"
    p.write_text("secret-person-name\n", encoding="utf-8")
    rd = Redactor(pii_list_path=p)
    assert rd.redact_text("see Secret-Person-Name here").text == "see " + REDACTED + " here"
    assert rd.pii_list_ok
    p.unlink()
    assert rd.reload_pii_list() is False and rd.pii_list_ok is False


def _make_index(tmp_path, values):
    sd = tmp_path / "secrets"
    sd.mkdir()
    for i, v in enumerate(values):
        (sd / f"s{i}").write_text(v + "\n", encoding="utf-8")
    idx = tmp_path / "state" / "redact" / "index"
    redact_index.write_index(sd, idx)
    return idx


def test_secrets_dir_index_value_anywhere_in_text(tmp_path):
    secret = "plain-" + "value-" + "9f8e7d6c"       # どの形の規則にも当たらない 8 文字以上の値
    idx = _make_index(tmp_path, [secret, "short"])
    rd = Redactor(index_path=idx)
    assert rd.index_ok
    r = rd.redact_text("log: [" + secret + "] done; also KEY=" + secret)
    assert secret not in r.text and r.text.count(REDACTED) == 2
    assert any(h.category == "index" for h in r.hits)
    assert rd.redact_text("word short here").text == "word short here"   # 8 バイト未満は索引にない


def test_secrets_dir_multiline_file_lines_are_indexed(tmp_path):
    sd = tmp_path / "secrets"
    sd.mkdir()
    (sd / "two").write_text("first-line-value-1\nsecond-line-value-2\n", encoding="utf-8")
    idx = tmp_path / "idx"
    redact_index.write_index(sd, idx)
    rd = Redactor(index_path=idx)
    assert rd.redact_text("x second-line-value-2 y").text == "x " + REDACTED + " y"


def test_unreadable_index_falls_back_to_other_rules(tmp_path):
    rd = Redactor(index_path=tmp_path / "missing")
    assert rd.index_ok is False
    assert rd.redact_text("tel 090-1234-5678").hits  # 他の基準は効く
    idx = _make_index(tmp_path, ["plain-value-12345"])
    rd.index_path = idx
    assert rd.reload_index_if_changed() is True and rd.index_ok


def test_index_removed_later_is_noticed(tmp_path):
    idx = _make_index(tmp_path, ["plain-value-12345"])
    rd = Redactor(index_path=idx)
    assert rd.index_ok
    idx.unlink()
    assert rd.reload_index_if_changed() is False
    assert rd.redact_text("plain-value-12345").text == "plain-value-12345"  # 索引が無いので他の基準だけ


def test_redact_obj_walks_and_does_not_mutate():
    rd = fx()
    src = {"a": ["tel 090-1234-5678", {"k": "ok", "n": 5}], "pass" + "word=" + "abcdefghij1": 1, "t": True}
    out, hits = rd.redact_obj(src)
    assert src["a"][0] == "tel 090-1234-5678"
    assert out["a"][0] == "tel " + REDACTED and out["a"][1] == {"k": "ok", "n": 5} and out["t"] is True
    assert any(k.startswith(REDACTED) for k in out) and hits
    assert "abcdefghijk" not in str(out)


def test_summary_has_no_values():
    r = fx().redact_text(SECRET_INPUTS["sk"])
    s = summarize(r.hits)
    assert s == {"token:prefixed": 1}


def test_extra_config_file(tmp_path):
    cfg = tmp_path / "redact.yaml"
    cfg.write_text("allow_email_domains: [example.org]\nextra_patterns: ['ZZ-[0-9]{6}']\n", encoding="utf-8")
    rd = Redactor(config_path=cfg)
    assert rd.redact_text("test-user@example.org").hits == []
    assert rd.redact_text("a@example.net").hits  # 設定で置き換えた一覧に無い
    assert rd.redact_text("code ZZ-123456").text == "code " + REDACTED


@pytest.mark.parametrize("text", [
    "@pytest.mark.parametrize",
    " @fixture",
    "token = read_secret_optional('x')",
    "authorization = request.headers.get('a')",
    "token = token_value",
    "token = headers[0].decode('ascii')",
    "if self._token:\n    headers['Authorization'] = value",
    "token: str\n    token_id: str",
    "secret: self.config.secret",
])
def test_code_expressions_are_not_redacted(text):
    result = Redactor(allow_email_domains=[]).redact_text(text)
    assert result.text == text and not result.hits


@pytest.mark.parametrize("text", [
    "fixture@example.invalid",
    "mail fixture@example.invalid end",
    "pass" + "word=hunter2hunter",
    "token = 'token_value'",
    'secret: "self.config.secret"',
    '{"api_key": "' + "abcdefgh12345" + '"}',
])
def test_email_and_assignment_values_are_redacted(text):
    result = Redactor(allow_email_domains=[]).redact_text(text)
    assert REDACTED in result.text and result.hits
