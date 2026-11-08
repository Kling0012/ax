"""秘密検査と置き換え（S-15.3、D-2.2 axcommon.redact）。

使う部品: timeline（書き込み時に置き換え）、memory-connector（当たったら拒否）、hq、intake、observer、gate.secret_scan。

基準（S-15.3）は 3 組。
  1. 鍵・トークンの形（接頭辞つき、JWT、秘密鍵、Bearer、代入、URL の認証情報、直前の語つきの長い列）
  2. 個人情報（メールアドレス、電話番号、郵便番号、住所、利用者が登録した一覧）
  3. 秘密置き場の索引（host/ax-redact-index.sh が作る。先頭行がソルト、以降が 1 行 1 ハッシュ）

置き換え: 当たった部分を `[REDACTED]` にする（元の長さを漏らさない）。重なった範囲は 1 つにまとめる。
当たった値そのものは、戻り値・例外・ログのどこにも出さない（種別と件数だけ）。

設定: 既定値はこのファイルに持つ。`config/redact.yaml`（AX_CONFIG_DIR）があれば読む（`allow_email_domains`、`extra_patterns`）。
索引が読めないときは、他の基準だけで動く（index_ok が False になる。呼び出し側が health.alert を書く）。

試験の入力は実行時に連結して作る（試験のソースに秘密の形の実値を書かない。S-15.3）。
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

REDACTED = "[REDACTED]"

DEFAULT_ALLOW_EMAIL_DOMAINS = (
    "example.com", "example.org", "example.net", "localhost", "users.noreply.github.com",
)

# --- 鍵・トークンの形 -------------------------------------------------------------------------

_PREFIXED = [
    r"sk-[A-Za-z0-9_-]{20,}",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"github_pat_[A-Za-z0-9_]{40,}",
    r"xox[abprs]-[A-Za-z0-9-]{10,}",
    r"AKIA[0-9A-Z]{16}",
    r"AIza[0-9A-Za-z_-]{35}",
    r"tk_[a-z0-9]{20,}",
    r"ibk_[A-Za-z0-9_-]{16,}",
    r"axg-[0-9a-f]{64}",
]
_RE_PREFIXED = re.compile(r"(?<![A-Za-z0-9_-])(?:" + "|".join(_PREFIXED) + r")")
_RE_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_RE_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL)
_RE_BEARER = re.compile(r"\bBearer\s+([A-Za-z0-9._~+/=-]{20,})")

# 代入に使う語（S-15.3 の 5）。語の直前は識別子の一部でもよい（my_secret=…）
_ASSIGN_WORDS = r"(?:api_key|apikey|secret|token|password|passwd|authorization)"
_RE_ASSIGN = re.compile(r"(?i)" + _ASSIGN_WORDS + r"[\"']?[ \t]*[:=][ \t]*(?![ \t])(?:[\"']|(?!(?:[A-Za-z_]\w*(?:\[[^]\n]+\]|\.[A-Za-z_]\w*)*)[ \t]*\())([^\s\"'`,;]{8,})")
_ASSIGN_SKIP_PREFIX = ("<", "${", "***", "[redacted]", "example", "changeme", "xxxx")
# 7: 直前の語が 5 の語のときだけ、32 桁以上の 16 進と 40 文字以上の英数字の列を秘密とする
_RE_LONG_AFTER_WORD = re.compile(
    r"(?i)(?<![A-Za-z0-9_])" + _ASSIGN_WORDS + r"[\"']?[\s:=]+[\"']?([0-9A-Fa-f]{32,}|[A-Za-z0-9]{40,})(?![A-Za-z0-9])")
_RE_URL_CRED = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:([^\s/@]+)@")

# --- 個人情報 ---------------------------------------------------------------------------------

_RE_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+@-])[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_RE_PHONE = [
    re.compile(r"(?<![\w-])0\d{1,4}-\d{1,4}-\d{3,4}(?!\d)"),
    re.compile(r"(?<![\w-])0[789]0\d{8}(?!\d)"),
    re.compile(r"(?<![\w])\+81[-\s]?\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}(?!\d)"),
]
PREFECTURES = (
    "北海道 青森県 岩手県 宮城県 秋田県 山形県 福島県 茨城県 栃木県 群馬県 埼玉県 千葉県 東京都 神奈川県 新潟県 富山県 石川県 福井県 山梨県 長野県 "
    "岐阜県 静岡県 愛知県 三重県 滋賀県 京都府 大阪府 兵庫県 奈良県 和歌山県 鳥取県 島根県 岡山県 広島県 山口県 徳島県 香川県 愛媛県 高知県 福岡県 "
    "佐賀県 長崎県 熊本県 大分県 宮崎県 鹿児島県 沖縄県").split()
_PREF_ALT = "|".join(PREFECTURES)
_RE_POSTAL = [
    re.compile(r"〒\s?\d{3}-\d{4}"),
    re.compile(r"(?<!\d)\d{3}-\d{4}(?=\s*(?:" + _PREF_ALT + r"))"),
]
_DIGITS = r"[0-9０-９一二三四五六七八九十〇]"
_RE_ADDRESS = re.compile(
    r"(?:" + _PREF_ALT + r")[^\s0-9０-９]{1,24}?[市区町村郡][^\s0-9０-９]{0,24}" + _DIGITS + r"+(?:[-－ー丁目番地号0-9０-９]*)")

# 索引で照らす語（S-15.3）
_RE_INDEX_WORD = re.compile(r"[A-Za-z0-9._~+/=:-]{8,}")


@dataclass(frozen=True)
class Hit:
    """当たりの種別と位置。値は持たない。"""
    category: str  # token / pii / index
    rule: str
    start: int
    end: int


@dataclass
class RedactResult:
    text: str
    hits: list[Hit] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.hits)

    def categories(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for h in self.hits:
            key = f"{h.category}:{h.rule}"
            out[key] = out.get(key, 0) + 1
        return out


def _load_config_yaml(path: Path) -> dict:
    try:
        import yaml  # 使うときだけ読み込む（ホストのスクリプトが axcommon を軽く読めるように）
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001  設定が読めなくても既定で動く（fail-soft）
        return {}


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """NFKC と小文字化をした文字列と、正規化後の各位置 -> 元の位置の対応を返す。"""
    out: list[str] = []
    mp: list[int] = []
    for i, ch in enumerate(text):
        n = unicodedata.normalize("NFKC", ch).casefold()
        for c in n:
            out.append(c)
            mp.append(i)
    return "".join(out), mp


class Redactor:
    """S-15.3 の検査器。

    index_path        秘密置き場の索引（`${ax_state_dir}/redact/index` の写し。先頭行がソルト）。None なら索引を使わない。
    pii_list          利用者が登録した文字列（本名、個人のメールアドレス、電話番号、住所）。値は repr に出さない。
    pii_list_path     1 行 1 件のファイル（`pii-list`）。読めなければ空として動く（pii_list_ok が False）。
    allow_email_domains  許可するメールのドメイン。None なら config/redact.yaml、無ければ既定。
    """

    def __init__(self, *, index_path: str | os.PathLike | None = None, pii_list: Iterable[str] = (),
                 pii_list_path: str | os.PathLike | None = None, allow_email_domains: Iterable[str] | None = None,
                 config_path: str | os.PathLike | None = None):
        self.index_path = Path(index_path) if index_path else None
        self._salt: str | None = None
        self._hashes: frozenset[str] = frozenset()
        self._index_mtime: float | None = None
        self.index_ok = self.index_path is None  # 索引を使わない設定は「正常」とみなす
        cfg = {}
        cp = Path(config_path) if config_path else Path(os.environ.get("AX_CONFIG_DIR", "/ax/config")) / "redact.yaml"
        if cp.is_file():
            cfg = _load_config_yaml(cp)
        domains = allow_email_domains if allow_email_domains is not None else cfg.get("allow_email_domains") or DEFAULT_ALLOW_EMAIL_DOMAINS
        self.allow_email_domains = frozenset(str(d).lower() for d in domains)
        self._extra = []
        for pat in cfg.get("extra_patterns") or []:
            try:
                self._extra.append(re.compile(str(pat)))
            except re.error:
                continue  # 壊れた追加の形は飛ばす
        self.pii_list_path = Path(pii_list_path) if pii_list_path else None
        self.pii_list_ok = True
        self._pii: list[str] = []
        self.set_pii_list(pii_list)
        if self.pii_list_path is not None:
            self.reload_pii_list()
        if self.index_path is not None:
            self.reload_index()

    def __repr__(self) -> str:  # 一覧や索引の中身を出さない
        return f"Redactor(index_ok={self.index_ok}, hashes={len(self._hashes)}, pii={len(self._pii)})"

    # --- 索引と一覧 -----------------------------------------------------------------------------
    def reload_index(self) -> bool:
        """索引を読み直す。読めなければ index_ok=False（直前の内容は捨てる。古い索引で安心させない）。"""
        if self.index_path is None:
            return True
        try:
            st = self.index_path.stat()
            text = self.index_path.read_text(encoding="utf-8")
        except OSError:
            self._salt, self._hashes, self._index_mtime = None, frozenset(), None
            self.index_ok = False
            return False
        lines = text.split("\n")
        salt = lines[0].strip()
        if not salt:
            self._salt, self._hashes, self._index_mtime = None, frozenset(), None
            self.index_ok = False
            return False
        self._salt = salt
        self._hashes = frozenset(h.strip() for h in lines[1:] if h.strip())
        self._index_mtime = st.st_mtime
        self.index_ok = True
        return True

    def reload_index_if_changed(self) -> bool:
        """索引の更新時刻が変わったときだけ読み直す。index_ok を返す。"""
        if self.index_path is None:
            return True
        try:
            m = self.index_path.stat().st_mtime
        except OSError:
            if self.index_ok:
                self.reload_index()
            return self.index_ok
        if m != self._index_mtime or not self.index_ok:
            self.reload_index()
        return self.index_ok

    def set_pii_list(self, items: Iterable[str]) -> None:
        norm = []
        for it in items:
            it = (it or "").strip()
            if len(it) >= 2:
                norm.append(unicodedata.normalize("NFKC", it).casefold())
        self._pii = sorted(set(norm), key=len, reverse=True)

    def reload_pii_list(self) -> bool:
        if self.pii_list_path is None:
            return True
        try:
            self.set_pii_list(self.pii_list_path.read_text(encoding="utf-8").splitlines())
            self.pii_list_ok = True
        except OSError:
            self.pii_list_ok = False
        return self.pii_list_ok

    # --- 検査 -----------------------------------------------------------------------------------
    def scan(self, text: str) -> list[Hit]:
        """当たりの一覧（位置つき、重なりはそのまま）。"""
        if not text:
            return []
        hits: list[Hit] = []
        add = hits.append
        for m in _RE_PREFIXED.finditer(text):
            add(Hit("token", "prefixed", m.start(), m.end()))
        for m in _RE_JWT.finditer(text):
            add(Hit("token", "jwt", m.start(), m.end()))
        for m in _RE_PEM.finditer(text):
            add(Hit("token", "private_key", m.start(), m.end()))
        for m in _RE_BEARER.finditer(text):
            add(Hit("token", "bearer", m.start(1), m.end(1)))
        for m in _RE_ASSIGN.finditer(text):
            val = m.group(1)
            if val.lower().startswith(_ASSIGN_SKIP_PREFIX):
                continue
            if text[m.start(1) - 1] not in "\"'" and (
                re.fullmatch(r"[A-Za-z_]+", val)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", val)
            ):
                continue
            add(Hit("token", "assign", m.start(1), m.end(1)))
        for m in _RE_LONG_AFTER_WORD.finditer(text):
            add(Hit("token", "long_after_word", m.start(1), m.end(1)))
        for m in _RE_URL_CRED.finditer(text):
            if m.group(1) == REDACTED or m.group(1).startswith(("${", "<")):
                continue
            add(Hit("token", "url_credential", m.start(1), m.end(1)))
        for rx in self._extra:
            for m in rx.finditer(text):
                add(Hit("token", "extra", m.start(), m.end()))
        hits.extend(self._scan_pii(text))
        hits.extend(self._scan_index(text))
        return hits

    def _scan_pii(self, text: str) -> Iterator[Hit]:
        # Co-Authored-By: で始まる行は、メールアドレスの検査の対象外（S-15.3）
        pos = 0
        for line in text.splitlines(keepends=True):
            end = pos + len(line)
            if not line.lstrip().lower().startswith("co-authored-by:"):
                for m in _RE_EMAIL.finditer(line):
                    if m.group(1).lower() not in self.allow_email_domains:
                        yield Hit("pii", "email", pos + m.start(), pos + m.end())
            pos = end
        for rx in _RE_PHONE:
            for m in rx.finditer(text):
                yield Hit("pii", "phone", m.start(), m.end())
        for rx in _RE_POSTAL:
            for m in rx.finditer(text):
                yield Hit("pii", "postal", m.start(), m.end())
        for m in _RE_ADDRESS.finditer(text):
            yield Hit("pii", "address", m.start(), m.end())
        if self._pii:
            norm, mp = _normalize_with_map(text)
            for item in self._pii:
                ascii_word = item.isascii() and item.isalnum()
                i = norm.find(item)
                while i != -1:
                    j = i + len(item)
                    left = i > 0 and norm[i - 1].isascii() and norm[i - 1].isalnum()
                    right = j < len(norm) and norm[j].isascii() and norm[j].isalnum()
                    if not ascii_word or not (left or right):
                        yield Hit("pii", "registered", mp[i], mp[j - 1] + 1)
                        i = norm.find(item, j)
                    else:
                        i = norm.find(item, i + 1)

    def _scan_index(self, text: str) -> Iterator[Hit]:
        if not (self._salt and self._hashes):
            return
        from .redact_index import value_hash

        for m in _RE_INDEX_WORD.finditer(text):
            if value_hash(self._salt, m.group(0)) in self._hashes:
                yield Hit("index", "secrets_dir", m.start(), m.end())
                continue
            # `名前=値`、`名前:値` の形でも値を照らす（語は `=` と `:` を含むため、全体の語だけでは外れる）
            for piece in re.finditer(r"[^=:]+", m.group(0)):
                if len(piece.group(0).encode("utf-8")) >= 8 and piece.group(0) != m.group(0) \
                        and value_hash(self._salt, piece.group(0)) in self._hashes:
                    yield Hit("index", "secrets_dir", m.start() + piece.start(), m.start() + piece.end())

    def contains_secret(self, text: str) -> bool:
        return bool(self.scan(text))

    def redact_text(self, text: str) -> RedactResult:
        hits = self.scan(text)
        if not hits:
            return RedactResult(text, [])
        spans = sorted((h.start, h.end) for h in hits)
        merged: list[list[int]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out, last = [], 0
        for s, e in merged:
            out.append(text[last:s])
            out.append(REDACTED)
            last = e
        out.append(text[last:])
        return RedactResult("".join(out), hits)

    # --- 入れ子のデータ -------------------------------------------------------------------------
    def redact_obj(self, obj: Any) -> tuple[Any, list[Hit]]:
        """dict / list / 文字列を再帰的に置き換えた写しと、当たりの一覧を返す。元のオブジェクトは変えない。
        文字列以外の値（数、真偽、null）はそのまま。辞書のキーも検査する（当たったキーは `[REDACTED]#n`）。"""
        hits: list[Hit] = []

        def walk(o: Any) -> Any:
            if isinstance(o, str):
                r = self.redact_text(o)
                hits.extend(r.hits)
                return r.text
            if isinstance(o, list):
                return [walk(x) for x in o]
            if isinstance(o, tuple):
                return [walk(x) for x in o]
            if isinstance(o, dict):
                res: dict = {}
                n = 0
                for k, v in o.items():
                    if isinstance(k, str):
                        rk = self.redact_text(k)
                        if rk.hits:
                            hits.extend(rk.hits)
                            n += 1
                            key = f"{REDACTED}#{n}"
                        else:
                            key = k
                    else:
                        key = k
                    res[key] = walk(v)
                return res
            return o

        return walk(obj), hits

    def as_hook(self) -> Callable[[Any], Any]:
        """axcommon.events.set_redactor に渡す関数（置き換えた写しだけを返す）。"""
        return lambda obj: self.redact_obj(obj)[0]


def summarize(hits: Iterable[Hit]) -> dict[str, int]:
    """拒否の応答やログに出してよい形（種別ごとの件数。値は含まない）。"""
    out: dict[str, int] = {}
    for h in hits:
        key = f"{h.category}:{h.rule}"
        out[key] = out.get(key, 0) + 1
    return out


def from_environment(environ: dict | None = None) -> Redactor:
    """環境変数から作る。AX_REDACT_INDEX（索引）、AX_PII_LIST（pii-list のファイル）、AX_CONFIG_DIR（redact.yaml の置き場）。"""
    env = os.environ if environ is None else environ
    return Redactor(index_path=env.get("AX_REDACT_INDEX") or None, pii_list_path=env.get("AX_PII_LIST") or None,
                    config_path=(Path(env["AX_CONFIG_DIR"]) / "redact.yaml") if env.get("AX_CONFIG_DIR") else None)
