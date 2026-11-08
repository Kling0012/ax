"""compose の volume.subpath を使えるかの判定（F-20「B2 での判定」）。

実機でしか通せない。ここには判定のロジックと、実機で流す手順（argv の列）だけを置き、実機の判定は未実行（status/b1.md）。
3 つを確かめ、3 つとも真なら subpath、1 つでも偽なら全ボリューム一律で独立した名前つきボリュームの代替（<サービス>-archive）にする。
  (1) archive-init が終了コード 0 で終わる
  (2) archiver が起動し、ls -A /vol/<ボリューム名> が archive/ の中身だけを見せる（親ボリュームの他のファイルとディレクトリが見えない）
  (3) /vol/<ボリューム名> に書いた試験ファイルが、ボリュームを持つサービス側の archive/ に現れる

使い方（実機。go の後、b2 の最初）:
  python3 -m core.archiver.subpath_probe plan <ボリューム名>      # 流す手順（docker compose の argv）を表示する。実行はしない
  python3 -m core.archiver.subpath_probe decide RESULT.json       # 手順の結果（下の形）から判定する
RESULT.json: {"init_rc": 0, "listing": ["ax-subpath-probe.txt"], "parent_names_seen": [], "owner_sees_file": true}
"""
from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass

PROBE_FILE = "ax-subpath-probe.txt"
PARENT_SENTINEL = "ax-parent-sentinel"


@dataclass(frozen=True)
class Decision:
    mode: str                  # "subpath" | "named_volume"
    checks: dict
    reasons: tuple[str, ...]


def evaluate(result: dict) -> Decision:
    """手順の結果から判定する。欠けた項目は偽として扱う（確かめられない物で subpath を採らない）。"""
    c1 = result.get("init_rc") == 0
    listing = result.get("listing")
    seen = result.get("parent_names_seen")
    c2 = isinstance(listing, list) and isinstance(seen, list) and not seen and set(listing) <= {PROBE_FILE}
    c3 = result.get("owner_sees_file") is True
    checks = {"init_exit_0": c1, "only_archive_visible": c2, "owner_sees_written_file": c3}
    reasons = tuple(k for k, v in checks.items() if not v)
    return Decision("subpath" if all(checks.values()) else "named_volume", checks, reasons)


def plan(volume: str, *, project: str = "ax", compose_args: str = "") -> list[list[str]]:
    """実機で流す手順。sudo -n と compose の引数（-f の列、--env-file）は呼び出し側が足す。ここでは docker compose の部分だけ。"""
    base = ["docker", "compose", "-p", project] + shlex.split(compose_args)
    return [
        base + ["run", "--rm", "archive-init"],                                        # (1) の終了コード
        base + ["run", "--rm", "--no-deps", "--entrypoint", "sh", "archiver", "-c",
                f"touch /vol/{volume}/{PROBE_FILE}; ls -A /vol/{volume}"],              # (2) の見え方と (3) の書き込み
        base + ["exec", "-T", volume.removesuffix("-data"), "ls", "-A", "archive"],       # (3) 持ち主の側から見える
    ]


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "plan":
        for cmd in plan(argv[1]):
            print(" ".join(shlex.quote(x) for x in cmd))
        return 0
    if len(argv) >= 2 and argv[0] == "decide":
        with open(argv[1], encoding="utf-8") as f:
            d = evaluate(json.load(f))
        print(json.dumps({"mode": d.mode, "checks": d.checks, "reasons": list(d.reasons)}, ensure_ascii=False))
        return 0 if d.mode == "subpath" else 3
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
