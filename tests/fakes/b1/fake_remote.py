"""偽の退避先（rrsync 付きの sshd の模擬。F-20、T-2）。ソケットも ssh も使わない。

rrsync の役目の模擬: 鍵に結び付いた強制コマンド `rrsync <root>` は、(1) rsync 以外の命令（シェル）を実行させず、(2) 書き込み先を <root> の下に限る。
  FakeRemote(root).runner  は archiver の Transport に渡す Runner。argv の中の `user@host:path` を見つけ、
  - path が <root> の外へ出る（絶対パス、`..`）なら、rrsync と同じく拒否する（rc=12、stderr に理由）
  - そうでなければ <root>/<path> に読み替え、`-e …`（ssh の引数）を外して、**本物の rsync をローカルで**実行する
    （--checksum、--files-from、--dry-run --itemize-changes の判定は本物の rsync の動き）。
  - 実行した全てを commands に残す。シェルや rsync 以外の命令は shell_attempts に残して拒否する。
tamper(path, data) は、照合の後で元のファイルを書き換える試験の補助ではなく、遠隔の中身を同じ大きさのまま書き換える補助（内容が違うコピー）。
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from core.archiver.rsync import RunResult

REMOTE_RE = re.compile(r"^([\w.-]+)@([\w.-]+):(.*)$")


@dataclass
class FakeRemote:
    root: Path
    commands: list[list[str]] = field(default_factory=list)
    shell_attempts: list[str] = field(default_factory=list)
    down: bool = False
    full: bool = False
    hook_after_send: object = None     # 送信が成功した直後に呼ぶ関数（照合の前に元を書き換える試験）
    corrupt_on_send: bool = False      # 送信の後で、遠隔の中身を同じ大きさのまま変える

    def __post_init__(self):
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    # --- rrsync の模擬 ----------------------------------------------------------------------------
    def confine(self, rel: str) -> Path | None:
        """rrsync と同じ。<root> の下だけ。絶対パスと .. は拒否。"""
        if rel.startswith("/"):
            return None
        parts = Path(rel).parts
        if ".." in parts:
            return None
        p = (self.root / rel)
        if os.path.commonpath([os.path.realpath(self.root), os.path.realpath(p)]) != os.path.realpath(self.root):
            return None
        return p

    def exec_command(self, cmd: str) -> RunResult:
        """この鍵で実行しようとした遠隔のコマンド。rsync のサーバー以外は強制コマンドが拒否する。"""
        self.shell_attempts.append(cmd)
        return RunResult(1, "", "rrsync: invalid command")

    # --- Runner ---------------------------------------------------------------------------------
    def runner(self, argv: list[str], *, cwd: str | None = None, timeout: float | None = None) -> RunResult:
        self.commands.append(list(argv))
        if argv[0] != "rsync":
            return self.exec_command(" ".join(argv))
        if self.down:
            return RunResult(255, "", "ssh: connect to host x port 2202: Connection refused")
        out = []
        remote_idx = None
        skip = False
        for i, a in enumerate(argv[1:], start=1):
            if skip:
                skip = False
                continue
            if a == "-e":
                skip = True
                continue
            if REMOTE_RE.match(a):
                remote_idx = i
        if remote_idx is None:
            return RunResult(1, "", "fake: 遠隔の指定が無い")
        m = REMOTE_RE.match(argv[remote_idx])
        rel = m.group(3)
        dest = self.confine(rel) if rel else self.root
        if dest is None:
            return RunResult(12, "", f"rrsync error: option {rel} is outside of the allowed directory")
        if self.full and "--dry-run" not in argv:
            return RunResult(11, "", "rsync: write failed: No space left on device (28)")
        local = []
        skip = False
        for i, a in enumerate(argv):
            if skip:
                skip = False
                continue
            if a == "-e":
                skip = True
                continue
            local.append(str(dest) + "/" if i == remote_idx else a)
        p = subprocess.run(local, cwd=cwd, capture_output=True, text=True, check=False)
        if p.returncode == 0 and "--dry-run" not in argv:
            if self.hook_after_send:
                self.hook_after_send()
            if self.corrupt_on_send:
                self.tamper_all()
        return RunResult(p.returncode, p.stdout, p.stderr)

    def tamper_all(self) -> None:
        for p in self.root.rglob("*"):
            if p.is_file() and p.suffix != ".json":
                self.tamper(p)

    @staticmethod
    def tamper(path: Path) -> None:
        data = path.read_bytes()
        if data:
            path.write_bytes(bytes([data[0] ^ 0x01]) + data[1:])      # 大きさは同じ、内容が違う
