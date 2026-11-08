"""偽の launcher（B6a の試験用）。ネットワークも SSH も使わない。

tests/b6b の FakeDocker が docker を偽るのと同じしたら、ここは ax-env-run の job-* を偽る。
呼び出し: transport(cmd, stdin) -> (exit_code, stdout)。exit_code は
ax-env-run のものと同じ（0 / 2 / 3 / 4 / 70）。

  fake = FakeLauncher()
  fake.transport_for(client)   # LauncherClient に渡す transport
  fake.start_ok("/job-1")      # 既定は job-start から {"job_id": ..., "root": ..., "wd": ...} を返す
  fake.session_missing()       # job-start が 3 （session_not_found）を返す
  fake.busy()                  # job-start が 4 （busy）を返す
  fake.calls                   # 受けた呼び出しの一覧（引数と stdin）
"""
from __future__ import annotations

import json


class FakeLauncher:
    def __init__(self):
        self.calls: list[dict] = []
        self._job_start: list[tuple[int, dict]] = []
        self._job_status: list[tuple[int, dict]] = []
        self._job_stop: list[tuple[int, dict]] = []
        self._job_retire: list[tuple[int, dict]] = []

    # --- 脚本 ---
    def start_ok(self, job_id: str = "", *, root: str | None = None, wd: str | None = None) -> "FakeLauncher":
        self._job_start.append((0, {"job_id": job_id, "root": root or job_id, "wd": wd or job_id}))
        return self

    def session_missing(self, *a, **kw) -> "FakeLauncher":
        self._job_start.append((3, {"ok": False, "error": {"class": "not_found", "reason": "session_not_found",
                                          "message": "会話の記録が無い"}}))
        return self

    def busy(self, *a, **kw) -> "FakeLauncher":
        self._job_start.append((4, {"ok": False, "error": {"class": "busy", "reason": "session_busy",
                                                          "message": "同じ連鎖で実行中のコンテナがある"}}))
        return self

    def rejected(self, reason: str = "unknown_field") -> "FakeLauncher":
        self._job_start.append((2, {"ok": False, "error": {"class": "rejected", "reason": reason, "message": "依頼の不備"}}))
        return self

    def status(self, code: int, obj: dict) -> "FakeLauncher":
        self._job_status.append((code, obj))
        return self

    def stop(self, code: int, obj: dict) -> "FakeLauncher":
        self._job_stop.append((code, obj))
        return self

    def retire(self, code: int, obj: dict) -> "FakeLauncher":
        self._job_retire.append((code, obj))
        return self

    # --- transport ---

    def transport(self, cmd: str, stdin: str):
        self.calls.append({"cmd": cmd, "stdin": stdin})
        parts = cmd.split()
        sub = next((p for p in parts if p.startswith("job-")), None)
        if sub == "job-start":
            req = json.loads(stdin or "{}")
            if self._job_start:
                code, doc = self._job_start.pop(0)
            else:
                code, doc = 0, {"job_id": req.get("job_id"), "root": req.get("job_id"), "wd": req.get("workdir_id") or req.get("job_id")}
            doc = dict(doc)
            if not doc.get("job_id"):
                doc["job_id"] = req.get("job_id")
            return code, json.dumps(doc)
        if sub == "job-status":
            if self._job_status:
                code, doc = self._job_status.pop(0)
                return code, json.dumps(doc)
            jid = parts[-1] if parts else ""
            return 0, json.dumps({"job_id": jid, "state": "exited", "exit_code": 0})
        if sub == "job-stop":
            if self._job_stop:
                code, doc = self._job_stop.pop(0)
                return code, json.dumps(doc)
            return 0, json.dumps({"job_id": parts[-1], "state": "exited", "exit_code": 0})
        if sub == "job-retire":
            if self._job_retire:
                code, doc = self._job_retire.pop(0)
                return code, json.dumps(doc)
            return 0, json.dumps({"job_id": parts[-1], "root": parts[-1], "archived": None})
        if sub == "job-list":
            return 0, json.dumps({"jobs": []})
        return 2, json.dumps({"ok": False, "error": {"class": "rejected", "reason": "unknown_subcommand"}})
