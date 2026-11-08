"""退避元と退避先の設定（F-20）。

退避元: profile の archive-sources.yaml（`sources:` の一覧。一覧だけのファイルも読む）。各項目は
  name、path（ホストの絶対パス。表示用。コンテナの中では /src/<name> に ro で bind される）、min_age_days（既定は S-17 archive_after_days）、
  include / exclude（glob、任意）、keep_original（glob、任意）、remove_original（既定 false）。
AX の各ボリュームの archive/ は、/vol/<ボリューム名> から vol-<ボリューム名> の名前で自動で加わる。
退避先: profile の archive_dest {host, port, user}（`/profile/<host>.yaml`）。環境変数 AX_ARCHIVE_DEST_HOST / _PORT / _USER で上書きできる。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9-]+$")
# ボリュームの archive/ の中で、書き出した索引などは元から除去しない（timeline の archive/timeline/index.json が消えると照会ができなくなる）
VOLUME_KEEP_DEFAULT = ("index.json", "*/index.json")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    name: str
    path: str                      # ホストの絶対パス（表示用）か、~ 付きのまま
    root: Path                     # コンテナの中の場所
    min_age_days: int
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    keep_original: tuple[str, ...] = ()
    remove_original: bool = False
    kind: str = "host"             # host（ro の bind。除去はホスト側の reap）/ volume（rw。archiver 自身が移す）


@dataclass(frozen=True)
class Dest:
    host: str
    port: int
    user: str

    def spec(self, rel: str) -> str:
        return f"{self.user}@{self.host}:{rel}"


def _tuple(v) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    return tuple(str(x) for x in v)


def parse_sources(doc, *, src_base: str | Path = "/src", default_min_age_days: int = 14) -> list[Source]:
    items = doc.get("sources") if isinstance(doc, dict) else doc
    if items is None:
        return []
    if not isinstance(items, list):
        raise ConfigError("archive-sources の形が違う（sources は一覧）")
    out: list[Source] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            raise ConfigError("archive-sources の項目が object でない")
        name = str(it.get("name", ""))
        if not NAME_RE.match(name) or name.startswith("vol-"):
            raise ConfigError(f"name が不正（英小文字・数字・ハイフン。vol- で始めない）: {name!r}")
        if name in seen:
            raise ConfigError(f"name が重複: {name}")
        seen.add(name)
        path = str(it.get("path", ""))
        if not (path.startswith("/") or path.startswith("~")):
            raise ConfigError(f"path は絶対パスか ~ で始める: {name}")
        age = it.get("min_age_days", default_min_age_days)
        if not isinstance(age, int) or isinstance(age, bool) or age < 0:
            raise ConfigError(f"min_age_days が不正: {name}")
        out.append(Source(name=name, path=path, root=Path(src_base) / name, min_age_days=age, include=_tuple(it.get("include")),
                          exclude=_tuple(it.get("exclude")), keep_original=_tuple(it.get("keep_original")),
                          remove_original=bool(it.get("remove_original", False)), kind="host"))
    return out


def volume_sources(vol_base: str | Path = "/vol", default_min_age_days: int = 14) -> list[Source]:
    base = Path(vol_base)
    out: list[Source] = []
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if d.is_dir() and not d.is_symlink() and NAME_RE.match(d.name):
            out.append(Source(name=f"vol-{d.name}", path=str(d), root=d, min_age_days=default_min_age_days,
                              keep_original=VOLUME_KEEP_DEFAULT, remove_original=True, kind="volume"))
    return out


def load_sources(sources_file: str | Path | None, *, src_base: str | Path = "/src", vol_base: str | Path = "/vol",
                 default_min_age_days: int = 14) -> list[Source]:
    import yaml

    host_sources: list[Source] = []
    if sources_file and Path(sources_file).is_file():
        doc = yaml.safe_load(Path(sources_file).read_text(encoding="utf-8"))
        host_sources = parse_sources(doc, src_base=src_base, default_min_age_days=default_min_age_days)
    return host_sources + volume_sources(vol_base, default_min_age_days)


def load_dest(profile_file: str | Path | None = None, environ: dict | None = None) -> Dest:
    env = os.environ if environ is None else environ
    host, port, user = env.get("AX_ARCHIVE_DEST_HOST"), env.get("AX_ARCHIVE_DEST_PORT"), env.get("AX_ARCHIVE_DEST_USER")
    if not (host and port and user) and profile_file and Path(profile_file).is_file():
        import yaml

        d = (yaml.safe_load(Path(profile_file).read_text(encoding="utf-8")) or {}).get("archive_dest") or {}
        host, port, user = host or d.get("host"), port or d.get("port"), user or d.get("user")
    if not (host and port and user):
        raise ConfigError("archive_dest（host、port、user）が決まらない")
    try:
        return Dest(str(host), int(port), str(user))
    except ValueError as e:
        raise ConfigError("archive_dest.port が整数でない") from e
