"""退避先への転送（F-20）。rsync を ssh 越しに呼ぶ。コマンドの組み立てと、結果の判定だけを持つ。

- 鍵: 専用の鍵（秘密 archive-ssh-key）、ホスト鍵の 1 行（秘密 archive-known-hosts）。退避先側は `restrict,command="rrsync <退避先>"` で書き込み先を限る。
  この鍵ではシェルも他の場所も使えないので、遠隔の操作はすべて rsync で行う（mkdir も rsync の暗黙のディレクトリで足す）。
- 転送: `rsync -a --checksum`（--delete は付けない）。ファイルの一覧は --files-from（NUL 区切り）で渡す。
- 内容の照合: 同じ引数に `--dry-run --itemize-changes` を付けて再実行し、差分が 0 件なら合格。--checksum は両側でファイル全体を読んで比べるので、
  大きさと件数が同じで内容が違うコピーは差分に出る（S-14.3）。ディレクトリの属性だけの行（`.d…`）は差分に数えない。
- 実行は Runner（argv -> RunResult）。試験は偽の Runner（tests/fakes/b1/fake_remote.py）を渡す。
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .config import Dest

SSH_KEY = "/run/secrets/archive-ssh-key"
KNOWN_HOSTS = "/run/secrets/archive-known-hosts"
RSYNC_BASE = ["rsync", "-a", "--checksum"]
IO_TIMEOUT_S = 300
ITEMIZE_RE = re.compile(r"^([<>ch.*][fdLDS]|\*deleting)")


@dataclass
class RunResult:
    rc: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def __call__(self, argv: list[str], *, cwd: str | None = None, timeout: float | None = None) -> RunResult: ...


def subprocess_runner(argv: list[str], *, cwd: str | None = None, timeout: float | None = None) -> RunResult:
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return RunResult(124, "", "timeout")
    except OSError as e:
        return RunResult(127, "", f"exec: {type(e).__name__}")
    return RunResult(p.returncode, p.stdout, p.stderr)


class TransferError(Exception):
    """転送か照合の失敗。reason は archive.failed の理由の語。detail は短い説明（秘密を含まない）。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


def classify_failure(rc: int, stderr: str) -> str:
    s = stderr.lower()
    if rc == 124:
        return "timeout"
    if "no space left" in s or "disk quota" in s:
        return "dest_full"
    if "permission denied (publickey" in s or "host key verification failed" in s or "remote host identification" in s:
        return "key_rejected"
    if rc == 255 or "connection refused" in s or "connection timed out" in s or "no route" in s or "could not resolve" in s or "unreachable" in s:
        return "unreachable"
    if "permission denied" in s or "operation not permitted" in s:
        return "dest_permission"
    return "rsync_error"


def parse_itemize(stdout: str) -> list[str]:
    """--itemize-changes の出力から、差分の行（内容か属性が違う物）を返す。ディレクトリの属性だけの行は除く。"""
    diffs = []
    for line in stdout.splitlines():
        line = line.rstrip()
        if not line or not ITEMIZE_RE.match(line):
            continue
        if line.startswith(".d"):
            continue  # ディレクトリの属性だけ
        diffs.append(line)
    return diffs


class Transport:
    """退避先への転送。dest_prefix を渡すと ssh を使わず、その場所（ローカル）への rsync にする（試験用）。"""

    def __init__(self, dest: Dest | None, runner: Runner = subprocess_runner, *, key: str = SSH_KEY, known_hosts: str = KNOWN_HOSTS,
                 dest_prefix: str | None = None, io_timeout_s: int = IO_TIMEOUT_S, run_timeout_s: float = 6 * 3600):
        self.dest, self.runner = dest, runner
        self.key, self.known_hosts = key, known_hosts
        self.dest_prefix = dest_prefix
        self.io_timeout_s, self.run_timeout_s = io_timeout_s, run_timeout_s

    # --- 組み立て -------------------------------------------------------------------------------
    def ssh_command(self) -> str:
        assert self.dest is not None
        return " ".join(shlex.quote(x) for x in [
            "ssh", "-p", str(self.dest.port), "-i", self.key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "ConnectTimeout=20"])

    def remote(self, rel: str) -> str:
        rel = rel.strip("/")
        if self.dest_prefix is not None:
            return str(Path(self.dest_prefix) / rel) + "/"
        assert self.dest is not None
        return self.dest.spec(rel + "/")

    def _argv(self, extra: list[str], source: str, target: str) -> list[str]:
        argv = list(RSYNC_BASE) + [f"--timeout={self.io_timeout_s}"] + extra
        if self.dest_prefix is None:
            argv += ["-e", self.ssh_command()]
        return argv + [source, target]

    def send_argv(self, src_root: Path, files_from: Path, dest_rel: str, *, dry_run: bool = False) -> list[str]:
        extra = ["--from0", f"--files-from={files_from}"]
        if dry_run:
            extra += ["--dry-run", "--itemize-changes"]
        return self._argv(extra, str(src_root).rstrip("/") + "/", self.remote(dest_rel))

    # --- 操作 -----------------------------------------------------------------------------------
    def _run(self, argv: list[str], cwd: str | None = None) -> RunResult:
        return self.runner(argv, cwd=cwd, timeout=self.run_timeout_s)

    def ensure_dirs(self, dest_rel: str) -> None:
        """遠隔の <name>/<日付>/ を作る。シェルが使えない鍵なので、空のディレクトリ木を --relative で送って作る。"""
        with tempfile.TemporaryDirectory(prefix="archiver-dirs-", dir=os.environ.get("TMPDIR")) as td:
            rel = dest_rel.strip("/")
            (Path(td) / rel).mkdir(parents=True, exist_ok=True)
            argv = list(RSYNC_BASE) + [f"--timeout={self.io_timeout_s}", "--relative"]
            if self.dest_prefix is None:
                argv += ["-e", self.ssh_command()]
            if self.dest_prefix is not None:
                Path(self.dest_prefix).mkdir(parents=True, exist_ok=True)
                target = str(Path(self.dest_prefix)) + "/"
            else:
                target = self.dest.spec("")  # type: ignore[union-attr]
            argv += [f"./{rel}/", target]
            r = self._run(argv, cwd=td)
        if r.rc != 0:
            raise TransferError(classify_failure(r.rc, r.stderr), f"ディレクトリを作れない rc={r.rc}")

    def send(self, src_root: Path, rel_paths: list[str], dest_rel: str) -> None:
        """rel_paths（src_root からの相対）を <dest_rel>/ へ送る。"""
        if not rel_paths:
            return
        with tempfile.TemporaryDirectory(prefix="archiver-list-", dir=os.environ.get("TMPDIR")) as td:
            lst = Path(td) / "files.list"
            lst.write_bytes(b"".join(p.encode("utf-8") + b"\0" for p in rel_paths))
            r = self._run(self.send_argv(src_root, lst, dest_rel))
        if r.rc != 0:
            raise TransferError(classify_failure(r.rc, r.stderr), f"rsync rc={r.rc}")

    def verify(self, src_root: Path, rel_paths: list[str], dest_rel: str) -> None:
        """同じ引数の --dry-run --itemize-changes が差分 0 件なら合格。そうでなければ TransferError(verify_mismatch)。"""
        if not rel_paths:
            return
        with tempfile.TemporaryDirectory(prefix="archiver-list-", dir=os.environ.get("TMPDIR")) as td:
            lst = Path(td) / "files.list"
            lst.write_bytes(b"".join(p.encode("utf-8") + b"\0" for p in rel_paths))
            r = self._run(self.send_argv(src_root, lst, dest_rel, dry_run=True))
        if r.rc != 0:
            raise TransferError(classify_failure(r.rc, r.stderr), f"照合の rsync が失敗 rc={r.rc}")
        diffs = parse_itemize(r.stdout)
        if diffs:
            raise TransferError("verify_mismatch", f"差分 {len(diffs)} 件")

    def put_file(self, local: Path, dest_rel_dir: str) -> None:
        """1 ファイルを <dest_rel_dir>/ へ送り、同じ引数の dry-run で照合する（manifest 用）。"""
        argv = self._argv([], str(local), self.remote(dest_rel_dir))
        r = self._run(argv)
        if r.rc != 0:
            raise TransferError(classify_failure(r.rc, r.stderr), f"manifest の送信に失敗 rc={r.rc}")
        chk = self._run(self._argv(["--dry-run", "--itemize-changes"], str(local), self.remote(dest_rel_dir)))
        if chk.rc != 0 or parse_itemize(chk.stdout):
            raise TransferError("verify_mismatch", "manifest の照合に失敗")
