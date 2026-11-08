"""profile から compose の変数への対応（F-18.6）。host/ax-env.sh が呼ぶ。標準ライブラリと PyYAML だけ。

CLI:
  python3 -m axcommon.profile --profile FILE [--out FILE] [--check]
終了コード 0: 成功 / 1: 必須の項目が無い、パスが相対、bind_addr が空など（何も書かない）。
B8 の仕上げで足す物（bind_addr が tailnet か 127.0.0.1 かの検査、hq_access の構造、型の検査、ax-hq の uid の取り込み、差分の表示）は、
この最小版に含めない（host/ax-env.sh の B8 担当が足す）。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# profile の項目 -> compose の変数（F-18.6 の表）。(項目, 変数, 種類)。種類: raw / path / dirname / relpath
MAPPING = [
    ("host", "AX_HOST", "raw"),
    ("bind_addr", "AX_BIND_ADDR", "raw"),
    ("secrets_dir", "AX_SECRETS_DIR", "path"),
    ("tmp_dir", "AX_TMP_DIR", "path"),
    ("ax_state_dir", "AX_STATE_DIR", "path"),
    ("commander_socket", "AX_COMMANDER_DIR", "dirname"),
    ("archive_target", "AX_ARCHIVE_TARGET", "raw"),
    ("memory_endpoint", "AX_MEMORY_ENDPOINT", "raw"),
    ("model_gateway", "AX_MODEL_GATEWAY", "raw"),
    ("ntfy_endpoint", "AX_NTFY_ENDPOINT", "raw"),
    ("ntfy_topic", "AX_NTFY_TOPIC", "raw"),
    ("environments_file", "AX_ENVIRONMENTS_FILE", "relpath"),
    ("models_file", "AX_MODELS_FILE", "relpath"),
    ("public_proxy_ip", "AX_PUBLIC_PROXY_IP", "raw"),
    # F-18.6 の表にない。D-14 の `${bind_addr}:${gate_port}` 用（OPEN.md の b0 の項目を見る）
    ("gate_port", "AX_GATE_PORT", "raw"),
]
# 必須の項目。F-18.6 は「必須の項目が無い」を失敗とするが、どれが必須かを書いていない。
# 表の項目のうち、環境ごとに値の無い物（public_proxy_ip と gate_port）を除く（docs/CHANGELOG.md、OPEN.md）。
REQUIRED = [m[0] for m in MAPPING if m[0] not in ("public_proxy_ip", "gate_port")]
DEFAULT_GATE_PORT = 8909
DEFAULT_AX_HOME = "~/.local/share/ax/prod"


class ProfileError(Exception):
    pass


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise ProfileError("PyYAML が無い") from e
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ProfileError(f"profile が読めない: {path}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise ProfileError(f"profile が YAML でない: {path}") from e
    if not isinstance(data, dict):
        raise ProfileError("profile の最上位がマッピングでない")
    return data


def _expand(value: str, ctx: dict[str, str]) -> str:
    """`~` と `${項目}`（profile の別の項目）を展開する。未知の ${…} はそのまま残す（compose の変数かもしれない）。"""
    if value.startswith("~"):
        value = os.path.expanduser(value)
    out = value
    for k, v in ctx.items():
        out = out.replace("${" + k + "}", v)
    return out


def build_env(profile: dict, environ: dict[str, str] | None = None) -> dict[str, str]:
    """profile から環境変数の dict を作る。不正なら ProfileError。"""
    environ = os.environ if environ is None else environ
    missing = [k for k in REQUIRED if profile.get(k) in (None, "")]
    if missing:
        raise ProfileError("必須の項目が無い: " + ", ".join(missing))
    bad_types = [k for k in REQUIRED if not isinstance(profile[k], str)]
    if bad_types:
        raise ProfileError("文字列でない項目: " + ", ".join(bad_types))
    if not str(profile["bind_addr"]).strip():
        raise ProfileError("bind_addr が空")
    # 先に ax_state_dir を展開し、他の項目の ${ax_state_dir} に使う
    ctx: dict[str, str] = {}
    ctx["ax_state_dir"] = _expand(profile["ax_state_dir"], {})
    for k in ("secrets_dir", "tmp_dir"):
        ctx[k] = _expand(profile[k], {"ax_state_dir": ctx["ax_state_dir"]})
    env: dict[str, str] = {}
    for key, var, kind in MAPPING:
        raw = profile.get(key)
        if raw in (None, ""):
            if key == "gate_port":
                env[var] = str(DEFAULT_GATE_PORT)
            else:
                env[var] = ""
            continue
        val = _expand(str(raw), ctx)
        if kind in ("path", "dirname"):
            if not os.path.isabs(val):
                raise ProfileError(f"{key} が相対パス: {raw}")
            if kind == "dirname":
                val = os.path.dirname(val)
        elif kind == "relpath":
            if os.path.isabs(val):
                raise ProfileError(f"{key} は AX_HOME からの相対パスにする: {raw}")
        env[var] = val
    ax_home = environ.get("AX_HOME") or os.path.expanduser(DEFAULT_AX_HOME)
    if not os.path.isabs(ax_home):
        raise ProfileError(f"AX_HOME が相対パス: {ax_home}")
    env["AX_HOME"] = ax_home
    env["AX_UID"] = str(os.getuid())
    env["AX_GID"] = str(os.getgid())
    env["AX_HQ_UID"] = ""  # B10 より前は空（B8 の仕上げで ax-hq の uid を取り込む）
    env["AX_HQ_GID"] = ""
    for k, v in env.items():
        if "\n" in v or "\r" in v:
            raise ProfileError(f"{k} の値に改行がある")
    return env


def render(env: dict[str, str]) -> str:
    lines = ["# host/ax-env.sh が profile から作った。手で直さない。秘密は入れない（F-18.6）。"]
    for k in sorted(env):
        lines.append(f"{k}={env[k]}")
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    """一時ファイルから mv（rename）で書く。権限は mode。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="axcommon.profile")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--print", dest="print_var", metavar="VAR", help="AX_ の変数 1 つの値だけを標準出力に出す（秘密でない変数だけ）")
    a = ap.parse_args(argv)
    try:
        prof = _load_yaml(Path(os.path.expanduser(a.profile)))
        env = build_env(prof)
    except ProfileError as e:
        print(f"ax-env: {e}", file=sys.stderr)
        return 1
    if a.print_var:
        if a.print_var not in env:
            print(f"ax-env: 知らない変数: {a.print_var}", file=sys.stderr)
            return 1
        print(env[a.print_var])
        return 0
    if a.check:
        print("ax-env: profile は有効")
        return 0
    out = a.out or os.path.join(env["AX_STATE_DIR"], "deploy", "profile.env")  # 既定は ${ax_state_dir}/deploy/profile.env
    try:
        write_atomic(Path(out), render(env))
    except OSError as e:
        print(f"ax-env: 書けない: {out}: {e.strerror}", file=sys.stderr)
        return 1
    print(f"ax-env: {out} を書いた（{len(env)} 変数。値は表示しない）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
