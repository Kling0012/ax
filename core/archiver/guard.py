"""退避の対象にしてはいけない物の検査（S-14.3）。

- 秘密置き場 secrets_dir とその配下（archive/ を含む）、x-cookie、一時・無効化済みの秘密は対象外。
- 対象を列挙するときは実パス（realpath）を確かめ、シンボリックリンクや別名経由でも秘密置き場に入らない。
- シンボリックリンクはたどらない（リンク自体も対象にしない）。退避元の root の外に出る実パスは対象外。
secrets_dir はコンテナに渡さない（S-2.2）。ここでの検査は二重の防御で、forbidden_roots（実パス）と forbidden_names を使う。
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_FORBIDDEN_NAMES = ("x-cookie",)
SECRET_PATH_WORDS = ("secrets",)


class SourceRefused(Exception):
    """退避元そのものが秘密置き場に入る（この退避元を丸ごと使わない）。"""


class Guard:
    def __init__(self, forbidden_roots=(), forbidden_names=DEFAULT_FORBIDDEN_NAMES):
        self.roots = [Path(os.path.realpath(r)) for r in forbidden_roots if r]
        self.names = frozenset(forbidden_names)

    @classmethod
    def from_environment(cls, environ: dict | None = None) -> "Guard":
        env = os.environ if environ is None else environ
        roots = [p for p in (env.get("AX_ARCHIVE_FORBIDDEN_ROOTS") or "").split(":") if p]
        roots += ["/run/secrets"]
        sd = env.get("AX_SECRETS_DIR_HOST")
        if sd:
            roots.append(sd)
        return cls(roots)

    def _inside_forbidden(self, real: Path) -> bool:
        for r in self.roots:
            if real == r or r in real.parents:
                return True
            # 実パスの上位に、禁止の場所が入っている形（秘密置き場が下にある）
            if real in r.parents:
                return True
        return False

    def check_source_root(self, root: Path) -> None:
        real = Path(os.path.realpath(root))
        for r in self.roots:
            if real == r or r in real.parents or real in r.parents:
                raise SourceRefused(f"退避元が秘密置き場に入る、または含む: {root.name}")

    def allowed(self, source_root: Path, path: Path) -> bool:
        """path（source_root の下のファイル）を対象にしてよいか。リンクは対象外。"""
        if path.is_symlink():
            return False
        real = Path(os.path.realpath(path))
        root_real = Path(os.path.realpath(source_root))
        if root_real not in real.parents:
            return False  # 親のどこかがリンクで、root の外へ出ている
        if self._inside_forbidden(real):
            return False
        rel_parts = real.relative_to(root_real).parts
        return not any(p in self.names for p in rel_parts)
