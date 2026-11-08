"""host/ax-env.sh の仕上げ（F-18.6 の B8 の列）: bind_addr の検査、hq_access の構造、S-16.3 の項目の型、ax-hq の uid・gid、差分の表示。

axcommon.profile（b0）の build_env が作った環境変数に、この検査と ax-hq の取り込みを足して、profile.env を書く。
値は標準出力に出さない（変えた変数の名前だけを出す）。失敗は何も書かずに終了コード 1。

  python3 -P -m hostwatch.envfinish --profile FILE [--check] [--out FILE]
環境変数（試験用の差し替え）: AX_ENV_TAILSCALE（tailnet のアドレスを出すコマンド。既定 "tailscale ip -4"）、AX_ENV_ID（id コマンド。既定 "id"）
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import shlex
import subprocess
import sys
from pathlib import Path

from axcommon import profile as prof

HQ_ACCESS_KEYS = ("iss", "jwks_url", "aud_secret", "allowed_users_file", "email_slot")
STR_ITEMS = ("host", "bind_addr", "secrets_dir", "tmp_dir", "archive_target", "environments_file", "memory_endpoint", "model_gateway",
             "models_file", "ntfy_endpoint", "ntfy_topic", "ax_state_dir", "commander_socket", "seat_user", "seat_socket", "seats_file",
             "seat_gate_path", "gate_peers_file", "archive_sources", "instabox_api_base", "instabox_machine_id", "box_machine_name",
             "box_machine_id", "public_proxy_ip", "entry_sites_file", "automation_map_source")
INT_ITEMS = {"gate_port": (1, 65535), "sandbox_gate_port": (1, 65535)}


class EnvError(Exception):
    pass


def _cmd_output(env_name: str, default: str) -> list[str] | None:
    cmd = shlex.split(os.environ.get(env_name) or default)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.split() if p.returncode == 0 else None


def check_types(raw: dict) -> None:
    """S-16.3 のすべての項目の型。"""
    for k in STR_ITEMS:
        if k in raw and not isinstance(raw[k], str):
            raise EnvError(f"{k} は文字列にする")
    for k, (lo, hi) in INT_ITEMS.items():
        if k in raw:
            v = raw[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise EnvError(f"{k} は {lo}〜{hi} の整数にする")
    cu = raw.get("commander_unit")
    if cu is not None and not (isinstance(cu, str) or (isinstance(cu, list) and all(isinstance(x, str) for x in cu))):
        raise EnvError("commander_unit は文字列か文字列の配列にする")
    for k in ("seat_socket", "seats_file", "seat_gate_path", "gate_peers_file"):
        if k in raw:
            value = prof._expand(raw[k], {"ax_state_dir": prof._expand(raw["ax_state_dir"], {})})
            if not os.path.isabs(value):
                raise EnvError(f"{k} は絶対パスにする")
    if "env_ssh_targets" in raw:
        t = raw["env_ssh_targets"]
        if not isinstance(t, list) or not all(isinstance(x, dict) and set(x) == {"id", "host", "port"} and isinstance(x["id"], str)
                                              and isinstance(x["host"], str) and type(x["port"]) is int and 1 <= x["port"] <= 65535 for x in t):
            raise EnvError("env_ssh_targets は {id, host, port} の配列にする")
    if "archive_dest" in raw:
        a = raw["archive_dest"]
        if not (isinstance(a, dict) and set(a) == {"host", "port", "user"} and isinstance(a["host"], str)
                and type(a["port"]) is int and 1 <= a["port"] <= 65535 and isinstance(a["user"], str)):
            raise EnvError("archive_dest は {host, port, user} にする")
    if "hq_access" in raw:
        h = raw["hq_access"]
        if not isinstance(h, dict):
            raise EnvError("hq_access はマッピングにする")
        missing = [k for k in HQ_ACCESS_KEYS if not (isinstance(h.get(k), str) and h[k].strip())]
        if missing:
            raise EnvError("hq_access に項目が無い・文字列でない: " + ", ".join(missing))


def check_bind_addr(addr: str) -> None:
    """tailnet のアドレス（tailscale ip -4 の出力）か 127.0.0.1 だけ（0.0.0.0 は不可。D-14）。"""
    try:
        ip = ipaddress.ip_address(addr.strip())
    except ValueError:
        raise EnvError("bind_addr がアドレスでない") from None
    if ip.is_unspecified:
        raise EnvError("bind_addr に 0.0.0.0 は使えない（tailnet のアドレスか 127.0.0.1）")
    if str(ip) == "127.0.0.1":
        return
    tail = _cmd_output("AX_ENV_TAILSCALE", "tailscale ip -4")
    if tail is None:
        raise EnvError("bind_addr が 127.0.0.1 でなく、tailnet のアドレスも確かめられない（tailscale ip -4 が使えない）")
    if str(ip) not in tail:
        raise EnvError("bind_addr が tailnet のアドレス（tailscale ip -4）でも 127.0.0.1 でもない")


def hq_ids() -> tuple[str, str]:
    """ax-hq の uid・gid。ユーザーが居なければ空（B10 より前）。"""
    base = shlex.split(os.environ.get("AX_ENV_ID") or "id")
    try:
        uid = subprocess.run([*base, "-u", "ax-hq"], capture_output=True, text=True, timeout=10)
        gid = subprocess.run([*base, "-g", "ax-hq"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "", ""
    if uid.returncode != 0 or gid.returncode != 0:
        return "", ""
    uid_text, gid_text = uid.stdout.strip(), gid.stdout.strip()
    if not uid_text.isdecimal() or not gid_text.isdecimal():
        raise EnvError("ax-hq の uid・gid が整数でない")
    return uid_text, gid_text


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k] = v
    except OSError:
        pass
    return out


def diff_names(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    return added, removed, changed


def build(profile_path: Path) -> dict[str, str]:
    raw = prof._load_yaml(profile_path)
    env = prof.build_env(raw)
    check_types(raw)
    check_bind_addr(str(raw["bind_addr"]))
    uid, gid = hq_ids()
    env["AX_HQ_UID"], env["AX_HQ_GID"] = uid, gid
    state = env["AX_STATE_DIR"]
    env["AX_ENTRY_SITES_FILE"] = prof._expand(raw.get("entry_sites_file", "${ax_state_dir}/views/sites.json"), {"ax_state_dir": state})
    env["AX_AUTOMATION_MAP_SOURCE"] = prof._expand(raw.get("automation_map_source", "${ax_state_dir}/views/map.json"), {"ax_state_dir": state})
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hostwatch.envfinish")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    try:
        env = build(Path(os.path.expanduser(a.profile)))
    except (prof.ProfileError, EnvError) as e:
        print(f"ax-env: {e}", file=sys.stderr)
        return 1
    if a.check:
        print("ax-env: profile は有効")
        return 0
    out = Path(a.out or os.path.join(env["AX_STATE_DIR"], "deploy", "profile.env"))
    added, removed, changed = diff_names(read_env_file(out), env)
    try:
        prof.write_atomic(out, prof.render(env))
    except OSError as e:
        print(f"ax-env: 書けない: {out}: {e.strerror}", file=sys.stderr)
        return 1
    print(f"ax-env: {out} を書いた（{len(env)} 変数。値は表示しない）")
    if added or removed or changed:
        parts = [f"{label}: {', '.join(names)}" for label, names in (("追加", added), ("削除", removed), ("変更", changed)) if names]
        print("ax-env: 差分（変数の名前だけ）: " + " / ".join(parts))
    else:
        print("ax-env: 差分なし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
