#!/usr/bin/env python3
"""切り替え作業の記録を 1 行追記する。保持 cutover を持つ者だけが書ける（S-24.1、FACTS.md の F-5.5）。

使い方: python3 -I cutover_note.py 種類 subject target result [reason]
種類は cutover.state / removal.state / removal.check。記録先は環境変数 AX_CUTOVER_DIR（既定 ~/.local/state/ax/cutover）。
subject が cutover-time の記録だけ、環境変数 CUTOVER_AT を data.at に入れる。
書く前に `ax claim renew cutover --json --request-id <新しい ULID>` を呼ぶ。席の ID と接続の ID は、その応答（入口が確かめた値）から写す。
引数・環境変数・標準入力から席の ID を受け取らない。標準ライブラリだけ。AX の機能ではなく、実施者の道具。
終了コード: 0 追記した（行の id を標準出力に出す）/ 2 引数が不正 / 3 ax が使えない・応答が読めない / 8 保持を持たない（2、3、8 は何も書かない）
"""
import datetime
import fcntl
import json
import os
import pathlib
import re
import secrets
import socket
import subprocess
import sys
import time

KINDS = ("cutover.state", "removal.state", "removal.check")
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # ULID（Crockford base32）
PRINCIPAL_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,31}")
ULID_RE = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
VIAS = ("local", "ssh", "tailnet", "hq")
MAX_LINE = 4000
AX_TIMEOUT_S = 30


def ulid():
    n = (int(time.time() * 1000) << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    return "".join(ALPHABET[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def main(argv=None, environ=None):
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    if len(argv) not in (4, 5) or argv[0] not in KINDS:
        die(2, "usage: cutover_note.py {%s} subject target result [reason]" % "|".join(KINDS))
    kind, subject, target, result = argv[:4]
    reason = argv[4] if len(argv) > 4 else ""
    if not subject or not target or (kind == "cutover.state" and result not in ("ok", "failed", "skipped", "waiting")):
        die(2, "subject、target、result が不正。何も書かない")
    at = environ.get("CUTOVER_AT", "")
    if kind == "cutover.state" and subject == "cutover-time":
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", at):
                raise ValueError
            datetime.datetime.strptime(at, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            die(2, "cutover-time にはミリ秒までの UTC の CUTOVER_AT が要る。何も書かない")
    # 1. 保持の確認と延長。入口の応答だけを根拠にする。
    try:
        p = subprocess.run(["ax", "claim", "renew", "cutover", "--json", "--request-id", ulid()],
                           capture_output=True, text=True, timeout=AX_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError):
        die(3, "ax claim renew cutover が使えない。何も書かない")
    if p.returncode == 8:
        die(8, "保持 cutover を持っていない。何も書かない")
    try:
        r = json.loads(p.stdout)
    except ValueError:
        die(3, "保持の確認の応答が読めない。何も書かない")
    err_code = r.get("error", {}).get("code") if isinstance(r, dict) and isinstance(r.get("error"), dict) else None
    if p.returncode == 8 or err_code in ("conflict", "preempted"):
        die(8, "保持 cutover を持っていない。ax claim acquire cutover を先に行う。何も書かない")
    d = r.get("data") if isinstance(r, dict) else None
    if p.returncode != 0 or not isinstance(r, dict) or not r.get("ok") or not isinstance(d, dict) or d.get("holder") is not True:
        die(3, "保持の確認の応答が読めない。何も書かない")
    o = d.get("owner") or {}
    if not isinstance(o, dict):
        die(3, "保持の確認の応答の形が違う。何も書かない")
    principal, session, via, cause = o.get("principal"), o.get("session"), o.get("via"), d.get("claim_event_id")
    if not (isinstance(principal, str) and PRINCIPAL_RE.fullmatch(principal)
            and isinstance(session, str) and 0 < len(session) <= 64 and session.isprintable()
            and via in VIAS
            and isinstance(cause, str) and ULID_RE.fullmatch(cause)):
        die(3, "保持の確認の応答の形が違う。何も書かない")
    # 2. 行を作って追記する。
    d_ = pathlib.Path(environ.get("AX_CUTOVER_DIR") or os.path.expanduser("~/.local/state/ax/cutover"))
    now = datetime.datetime.now(datetime.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    data = {"target": target, "external_item_id": None, "destination": None, "delivery_state": None,
            "active_sender": None, "result": result, "reason": reason}
    if kind != "cutover.state":
        data = {"component_id": target, "result": result, "reason": reason}
    if kind == "cutover.state" and subject == "cutover-time" and environ.get("CUTOVER_AT"):  # 切り替えの時刻（subject が cutover-time の記録だけが使う）
        data["at"] = environ["CUTOVER_AT"]
    ev = {"id": ulid(), "ts": ts,
          "source": {"component": "cutover-operator", "host": socket.gethostname(),
                     "principal": principal, "session": session, "via": via},
          "kind": kind, "subject": subject, "project": None, "subloop": None, "data": data, "cause": cause}
    line = json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"
    if len(line.encode()) >= MAX_LINE:
        die(2, "行が 4000 バイト以上。reason を短くする。何も書かない")
    d_.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d_, 0o700)
    fd = os.open(d_ / "cutover-state.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        os.fchmod(f.fileno(), 0o600)
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    print(ev["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
