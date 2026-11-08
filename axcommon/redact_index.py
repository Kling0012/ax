"""秘密置き場の索引の作成（S-15.3、F-18.6 の host/ax-redact-index.sh の本体）。標準ライブラリだけ。

索引: 先頭行がソルト、以降が 1 行 1 ハッシュ（重複なし、並べ替え）。権限 0644。
ハッシュ: sha256(salt の文字列の UTF-8 バイト列 + 値の UTF-8 バイト列) の 16 進。
対象の値: secrets_dir の下の全ファイル（シンボリックリンクはたどらない）の、ファイル全体（末尾の改行を除く）と各行（前後の空白を除く）のうち 8 バイト以上の物。
ファイルの中身を標準出力に出さない。

axcommon.redact（別の束）は value_hash() を使って語を照らす。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

MIN_BYTES = 8
MAX_FILE_BYTES = 4 * 1024 * 1024  # これを超えるファイルは索引に入れない（鍵の束などの異常な大きさを避ける）


def value_hash(salt: str, value: str) -> str:
    return hashlib.sha256(salt.encode("utf-8") + value.encode("utf-8")).hexdigest()


def new_salt() -> str:
    return secrets.token_hex(16)  # 16 バイトの乱数の 16 進


def read_salt(index_path: Path) -> str | None:
    """既存の索引があればそのソルトを返す（引き継ぐ）。"""
    try:
        first = index_path.read_text(encoding="utf-8").split("\n", 1)[0].strip()
    except OSError:
        return None
    return first or None


def iter_values(text: str):
    whole = text.rstrip("\r\n")
    if len(whole.encode("utf-8")) >= MIN_BYTES:
        yield whole
    for line in text.splitlines():
        v = line.strip()
        if len(v.encode("utf-8")) >= MIN_BYTES:
            yield v


class IndexReadError(Exception):
    """A source could not be read; do not expose its name or contents."""


def collect_values(secrets_dir: Path, *, stats: dict | None = None):
    def walk_error(_error):
        raise IndexReadError("secret source unreadable") from None

    for root, dirs, files in os.walk(secrets_dir, followlinks=False, onerror=walk_error):
        dirs.sort()
        for name in sorted(files):
            p = Path(root) / name
            try:
                st = p.lstat()
                if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
                    continue
                data = p.read_bytes()
            except OSError:
                raise IndexReadError("secret source unreadable") from None
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                if stats is not None:
                    stats["non_utf8"] = stats.get("non_utf8", 0) + 1
                continue
            yield from iter_values(text)


def build_index_text(secrets_dir: Path, salt: str, *, stats: dict | None = None) -> str:
    hashes = sorted({value_hash(salt, v) for v in collect_values(secrets_dir, stats=stats)})
    return "\n".join([salt, *hashes]) + "\n"


def write_index(secrets_dir: Path, index_path: Path, *, stats: dict | None = None) -> int:
    """索引を一時ファイルから mv で書く。作ったハッシュの件数を返す。"""
    salt = read_salt(index_path) or new_salt()
    text = build_index_text(secrets_dir, salt, stats=stats)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    owner = index_path.stat() if index_path.exists() else index_path.parent.stat()
    fd, tmp = tempfile.mkstemp(prefix=index_path.name + ".", dir=str(index_path.parent))
    try:
        if os.geteuid() == 0:
            os.fchown(fd, owner.st_uid, owner.st_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, index_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return text.count("\n") - 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="axcommon.redact_index")
    ap.add_argument("--secrets-dir", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    sd = Path(a.secrets_dir)
    if not sd.is_dir():
        print(f"ax-redact-index: 秘密置き場が無い: {sd}", file=sys.stderr)
        return 1
    try:
        stats = {}
        n = write_index(sd, Path(a.out), stats=stats)
    except IndexReadError:
        print("ax-redact-index: 秘密置き場を読み取れないため更新を中止", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"ax-redact-index: 書けない: {e.strerror}", file=sys.stderr)
        return 1
    print(f"ax-redact-index: {n} 件、UTF-8 対象外 {stats.get('non_utf8', 0)} 件（値は表示しない）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
