"""資格の読み込み（B0）。秘密の値はログ・例外・repr に出さない（S-15.2）。

- read_secret(name): compose の secrets（/run/secrets/<name>）から読む。環境変数 AX_SECRETS_PATH で置き場を変えられる（試験）。
  末尾の改行と空白を除く。無ければ SecretMissing（名前だけを含み、値は含まない）。
- TokenTable: 「<名前> <sha256 の16進>」を 1 行 1 件で持つ表。受けたトークンの sha256 を定数時間で比べて名前を返す。
  core-tokens.sha256（F-18.4）が使う。プラグインのトークンの一覧も同じ形で読めるが、その一覧のファイル名は文書で確定していない（OPEN.md）。
- parse_bearer(): Authorization ヘッダーから Bearer の値を取り出す。
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

DEFAULT_SECRETS_PATH = "/run/secrets"


class SecretMissing(Exception):
    pass


def secrets_path() -> Path:
    return Path(os.environ.get("AX_SECRETS_PATH", DEFAULT_SECRETS_PATH))


def read_secret(name: str, *, base: str | os.PathLike | None = None) -> str:
    if "/" in name or name in ("", ".", ".."):
        raise SecretMissing(f"秘密の名前が不正: {name!r}")
    p = Path(base) if base else secrets_path()
    try:
        return (p / name).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SecretMissing(f"秘密 {name} が読めない（{type(e).__name__}）") from None


def read_secret_optional(name: str, *, base: str | os.PathLike | None = None) -> str | None:
    try:
        return read_secret(name, base=base)
    except SecretMissing:
        return None


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_bearer(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


class TokenTable:
    """名前 -> sha256。ファイルは起動時と一定間隔で読み直す（reload_if_changed）。"""

    def __init__(self, entries: dict[str, str] | None = None, path: str | os.PathLike | None = None):
        self._entries = dict(entries or {})
        self.path = Path(path) if path else None
        self._mtime: float | None = None

    @classmethod
    def from_file(cls, path: str | os.PathLike) -> "TokenTable":
        t = cls(path=path)
        t.reload()
        return t

    def reload(self) -> bool:
        """読み直す。読めなければ False（表は変えない。fail-soft）。"""
        if self.path is None:
            return False
        try:
            text = self.path.read_text(encoding="utf-8")
            self._mtime = self.path.stat().st_mtime
        except OSError:
            return False
        entries: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 2 or len(parts[1]) != 64:
                continue  # 形の違う行は飛ばす
            entries[parts[0]] = parts[1].lower()
        self._entries = entries
        return True

    def reload_if_changed(self) -> bool:
        if self.path is None:
            return False
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return False
        if m != self._mtime:
            return self.reload()
        return False

    def names(self) -> list[str]:
        return sorted(self._entries)

    def identify(self, token: str | None) -> str | None:
        """トークンから名前を決める。一致なしは None。全件を同じ手間で比べる（定数時間）。"""
        if not token:
            return None
        h = sha256_hex(token)
        found = None
        for name, digest in self._entries.items():
            if hmac.compare_digest(h, digest) and found is None:
                found = name
        return found

    def __repr__(self) -> str:
        return f"TokenTable({len(self._entries)} entries)"
