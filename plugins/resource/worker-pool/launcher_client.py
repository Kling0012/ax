"""launcher の SSH クライアント（S-8.11、F-18.2、D-7.1）。

worker-pool のコンテナは、AX の専用ネットワーク上の SSH（宛先はそのネットワークの
ゲートウェイ）で launcher を呼ぶ。 forzado コマンドと専用の鍵（launcher-ssh-key）。
launcher は sudo -n docker run を行う。

- 呼び出し元が指定できるのは、runner の名前、モデル、許可先のホスト名、時間、
  環境変数の名前、読み取り専用の対象（target_ro、および登録簿の id で選ぶ
  追加のマウント ro_mounts）と session_id、resume_from、workdir_id、verify、tools だけ。
  イメージ、マウント、ネットワーク、コマンドは指定できない。
- 終了コード: 0 成功 / 2 依頼の不備（rejected） / 3 見つからない / 4 満枠（busy） / 70 内部の失敗。

試験は偽の launcher（tests/fakes/b6a/launcher_fake.py）で通す。ネットワークは使わない。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from axcommon.schemas import errors as _schema_errors

DEFAULT_LAUNCHER_USER = "operator"
DEFAULT_LAUNCHER_PORT = 22
DEFAULT_KEY_NAME = "launcher-ssh-key"
DEFAULT_KNOWN_HOSTS_NAME = "launcher-known-hosts"


class LauncherError(Exception):
    def __init__(self, exit_code: int, reason: str, payload: dict | None = None):
        super().__init__(f"launcher {exit_code}: {reason}")
        self.exit_code = exit_code
        self.reason = reason
        self.payload = payload or {}


def _validate_request(req: dict) -> list[str]:
    """job-start の依頼の項目の検査（S-8.11、F-18.2）。スキーマと合わせて確かめる。"""
    try:
        return _schema_errors("ext/b6a/job-launcher", req)
    except Exception:  # noqa: BLE001  スキーマが無い環境でも上の項目の検査は通す
        bad: list[str] = []
        if not re.match(r"^[a-z0-9-]{1,40}$", str(req.get("job_id", ""))):
            bad.append("job_id が不正")
        if not isinstance(req.get("allow_hosts"), list):
            bad.append("allow_hosts が無い")
        return bad


class LauncherClient:
    """launcher の呼び出し口。runner を SSH で呼ぶ。

    The SSH user defaults to operator and can be set with AX_LAUNCHER_USER.

    transport があれば、その呼び出し可能な物を呼ぶ（試験の偽物）。
    無ければ設定された接続先に ssh で接続する。
    """

    def __init__(self, *, host: str | None = None, user: str | None = None, port: int | None = None,
                 key_path: str | os.PathLike | None = None, known_hosts: str | os.PathLike | None = None,
                 transport=None, timeout: int = 180):
        self.host = host or os.environ.get("AX_LAUNCHER_HOST")
        self.user = user or os.environ.get("AX_LAUNCHER_USER") or DEFAULT_LAUNCHER_USER
        self.port = int(port or os.environ.get("AX_LAUNCHER_PORT") or DEFAULT_LAUNCHER_PORT)
        self.key_path = Path(key_path) if key_path else Path("/run/secrets") / DEFAULT_KEY_NAME
        self.known_hosts = Path(known_hosts) if known_hosts else Path("/run/secrets") / DEFAULT_KNOWN_HOSTS_NAME
        self.transport = transport
        self.timeout = timeout

    # --- 標準入力に JSON を渡す job-start -------------------------------------------
    def job_start(self, req: dict) -> dict:
        bad = _validate_request(req)
        if bad:
            raise LauncherError(2, "bad_request", {"errors": bad})
        return self._call("job-start", req["job_id"], stdin=json.dumps(req, ensure_ascii=False))

    def job_snapshot(self, snapshot_id: str, request: dict) -> dict:
        if not re.fullmatch(r"[a-z0-9-]{1,40}", snapshot_id):
            raise ValueError("invalid_snapshot_id")
        return self._call("job-snapshot", snapshot_id, stdin=json.dumps(request, ensure_ascii=False))

    def job_verify_recipe(self, request_id: str, request: dict) -> dict:
        if not re.fullmatch(r"[a-z0-9-]{1,40}", request_id):
            raise ValueError("invalid_request_id")
        return self._call("job-verify-recipe", request_id, stdin=json.dumps(request, ensure_ascii=False))

    def job_verify_prepare(self, job_id: str, request: dict) -> dict:
        if not re.fullmatch(r"[a-z0-9-]{1,40}", job_id):
            raise ValueError("invalid_job_id")
        return self._call("job-verify-prepare", job_id, stdin=json.dumps(request, ensure_ascii=False))

    def job_verify_result(self, job_id: str) -> dict:
        if not re.fullmatch(r"[a-z0-9-]{1,40}", job_id):
            raise ValueError("invalid_job_id")
        return self._call("job-verify-result", job_id)

    def job_chat_result(self, job_id: str) -> dict:
        if not re.fullmatch(r"[a-z0-9-]{1,40}", job_id):
            raise ValueError("invalid_job_id")
        return self._call("job-chat-result", job_id)

    def job_report(self, job_id: str) -> dict:
        if not re.fullmatch(r"[a-z0-9-]{1,40}", job_id):
            raise ValueError("invalid_job_id")
        return self._call("job-report", job_id)

    def job_status(self, job_id: str) -> dict:
        return self._call("job-status", job_id)

    def job_log(self, job_id: str, limit: int = 65536) -> dict:
        if not re.fullmatch(r"[a-z0-9-]{1,40}", job_id) or type(limit) is not int or not 1 <= limit <= 131072:
            raise ValueError("invalid_log_request")
        return self._call("job-log", f"{job_id} {limit}")

    def job_stop(self, job_id: str) -> dict:
        return self._call("job-stop", job_id)

    def job_retire(self, job_id: str) -> dict:
        return self._call("job-retire", job_id)

    def job_list(self) -> dict:
        return self._call("job-list", "")

    def _call(self, sub: str, job_id: str, *, stdin: str = "") -> dict:
        cmd = f"ax-env-run --caller launcher {sub}" + (f" {job_id}" if job_id else "")
        if self.transport is not None:
            code, out = self.transport(cmd, stdin)
        else:
            code, out = self._ssh(cmd, stdin)
        if not out:
            raise LauncherError(code, "empty_response")
        try:
            doc = json.loads(out)
        except ValueError:
            raise LauncherError(code, "bad_response")
        if code != 0:
            err = (doc or {}).get("error") or {}
            raise LauncherError(code, str(err.get("reason") or "launcher_error"), doc)
        return doc

    def _ssh(self, cmd: str, stdin: str) -> tuple[int, str]:
        missing = [name for name, value in (("AX_LAUNCHER_HOST", self.host), ("AX_LAUNCHER_USER", self.user)) if not value]
        if missing:
            raise LauncherError(70, "missing_config:" + ",".join(missing))
        argv = ["ssh", "-p", str(self.port), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        if self.key_path:
            argv += ["-i", str(self.key_path)]
        if self.known_hosts:
            argv += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        argv += [f"{self.user}@{self.host}", cmd]
        try:
            r = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LauncherError(70, f"ssh_unavailable_{type(e).__name__}")
        return r.returncode, (r.stdout or "").strip()

    def __repr__(self) -> str:
        return f"LauncherClient({self.user}@{self.host}:{self.port})"
