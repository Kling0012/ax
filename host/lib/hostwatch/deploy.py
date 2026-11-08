"""中核の差し替えとサブループの追加の配備（S-16.7、F-18.3 の 4）。host/ax-deploy.sh の本体。ホスト見張りが要求 `deploy` を読んだときに実行する。

手順（F-18.3。(b) の確認を取り込み (a) の前に置く。印のない main を AX_HOME に入れないため。DECISIONS.md）:
  (a) git fetch local main（と accepted/* のタグ）
  (b) 取り込もうとする版にタグ accepted/<sha> が無ければ deploy.rejected を書いて終わる（何も変えない。終了コード 10）
  (a') git merge --ff-only FETCH_HEAD
  (c) current.env を previous.env へ cp -p してから、新しい AX_TAG（main の短い ID 12 桁）を current.env に書く
  (d) vendor/claude を置き、docker compose build（--profile build の作業用イメージを含む）。失敗なら previous.env を戻して終わる
  (e) 変わった・新しいサービスを host/ax-up.sh <サービス名> で 1 つずつ起動する（登録済みか新しい物だけ。登録のない部品は起動しない）
  (f) 変わった中核のサービスの healthz が通らなければ、previous.env の AX_TAG で同じサービスを起動し直し、deploy.rolled_back を書く
      （オブザーバーが ToDo parked にする）。サブループの追加の失敗は、そのサービスだけ起動せず（登録しない）、他は続ける
終了コード: 0 完了（変更なしを含む）/ 1 失敗 / 4 別の配備が実行中 / 10 印がなく rejected / 11 中核が通らず前のタグへ戻した / 12 サブループの一部が起動しなかった
実機（docker compose build、起動）は実環境の試験（T-104 の実機部分、b17）。試験は偽の Runner で、組み立てたコマンドと分岐を確かめる。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path

from axcommon.profile import write_atomic

from . import compose as cx
from .config import HostConfig
from .hostlog import HostLog
from .runner import Result, SubprocessRunner

EXIT_OK, EXIT_FAILED, EXIT_BUSY, EXIT_REJECTED, EXIT_ROLLED_BACK, EXIT_PARTIAL = 0, 1, 4, 10, 11, 12
TAG_RE = re.compile(r"^accepted/([0-9a-f]{7,40})$")
COMMON_PATHS = ("axcommon/", "schemas/", "config/", "docker/", "pyproject.toml", "uv.lock")


def accepted_tag_for(tags: list[str], sha: str) -> str | None:
    """sha の版に付いた accepted/<sha> のタグ。<sha> は 7〜40 桁の先頭一致。"""
    for t in tags:
        m = TAG_RE.match(t.strip())
        if m and sha.startswith(m.group(1)):
            return t.strip()
    return None


def _norm(defn: dict, tag: str) -> str:
    return json.dumps(defn, sort_keys=True, ensure_ascii=False).replace(tag, "<TAG>") if tag else json.dumps(defn, sort_keys=True, ensure_ascii=False)


def _command_text(defn: dict) -> str:
    c = defn.get("command")
    return " ".join(c) if isinstance(c, list) else str(c or "")


def code_changed(svc: str, defn: dict, paths: list[str]) -> bool:
    image = str(defn.get("image", ""))
    for p in paths:
        if image.startswith("ax/") and p.startswith(COMMON_PATHS):
            return True
        m = re.match(r"^core/([^/]+)/", p)
        if m and not svc.startswith("sl-"):
            d = m.group(1)
            cmd = _command_text(defn)
            if f"core.{d.replace('-', '_')}" in cmd or f"core/{d}/" in cmd or svc == d:
                return True
        m = re.match(r"^plugins/(?:subloop|resource|timer)/([^/]+)/", p) or re.match(r"^plugins/([^/]+)/", p)
        if m and (svc == "sl-" + m.group(1) or svc == m.group(1)
                  or (svc == "timer-runner" and p.startswith("plugins/timer/"))):
            return True
        if p.startswith("plugins/nodes/") and svc.startswith("sl-"):
            return True
        if p.startswith("runtime/") and not p.startswith("runtime/images/") and (svc.startswith("sl-") or svc == "timer-runner"):
            return True
    return False


def changed_services(old: dict[str, dict], new: dict[str, dict], old_tag: str, new_tag: str, paths: list[str],
                     registered: set[str]) -> tuple[list[str], list[str]]:
    """(起動し直す順の一覧, 新しいサービスの一覧)。登録済みで変わった物と、新しい物だけ。一度きりのサービス（restart: "no"）は除く。"""
    out: list[str] = []
    fresh: list[str] = []
    for svc, d in new.items():
        if str(d.get("restart", "")) == "no" or svc.startswith("img-") or d.get("profiles") == ["build"]:
            continue
        if svc not in old:
            fresh.append(svc)
            out.append(svc)
        elif svc in registered and (_norm(d, new_tag) != _norm(old[svc], old_tag) or code_changed(svc, d, paths)):
            out.append(svc)
    out.sort(key=lambda s: (s != "observer", s.startswith("sl-"), s))
    return out, fresh


def is_core(svc: str) -> bool:
    return not svc.startswith("sl-")


class Deploy:
    def __init__(self, cfg: HostConfig, runner=None, log: HostLog | None = None):
        self.cfg = cfg
        self.run = (runner or SubprocessRunner()).run
        self.log = log or HostLog(cfg.log_path, cfg.host)
        self.home = cfg.ax_home
        self.deploy_dir = cfg.state_dir / "deploy"

    # --- 補助 ---
    def git(self, *args: str, timeout: float = 300) -> Result:
        return self.run(["git", "-C", str(self.home), *args], timeout=timeout)

    def _read_tag(self, path: Path) -> str:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("AX_TAG="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""

    def _write_current(self, tag: str) -> None:
        self.deploy_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.deploy_dir / f"current.env.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        current = self.deploy_dir / "current.env"
        try:
            lines = current.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        replaced = False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in lines:
                if line.startswith("AX_TAG="):
                    line = f"AX_TAG={tag}"
                    replaced = True
                f.write(line + "\n")
            if not replaced:
                f.write(f"AX_TAG={tag}\n")
        os.replace(tmp, current)

    def _restore_previous(self) -> None:
        prev = self.deploy_dir / "previous.env"
        if prev.exists():
            shutil.copy2(prev, self.deploy_dir / "current.env")

    def _up(self, svc: str) -> Result:
        env = {**os.environ, "AX_HOME": str(self.home), "AX_UP_SUDO": shlex.join(self.cfg.sudo), "AX_UP_NO_DEPS": "1"}
        return self.run(["bash", str(self.cfg.repo / "host" / "ax-up.sh"), "--profile", str(self.cfg.profile_path), svc], timeout=self.cfg.up_wait_s + 600, env=env)

    def _config_json(self) -> dict[str, dict] | None:
        r = self.run(self.cfg.compose("config", "--format", "json"), timeout=self.cfg.compose_timeout_s)
        if not r.ok:
            return None
        try:
            return cx.parse_config_json(r.out)
        except (ValueError, TypeError):
            return None

    def _jsonschema_version(self) -> str:
        """明示設定を優先し、なければ配備対象の uv.lock から版を得る。"""
        configured = os.environ.get("AX_JSONSCHEMA_VERSION", "")
        if not configured:
            result = self.run(self.cfg.compose("config", "--environment"),
                              timeout=self.cfg.compose_timeout_s)
            if not result.ok:
                raise ValueError("compose environment unavailable")
            for line in result.out.splitlines():
                if line.startswith("AX_JSONSCHEMA_VERSION="):
                    configured = line.partition("=")[2]
                    break
        if configured:
            return configured
        try:
            with (self.home / "uv.lock").open("rb") as f:
                packages = tomllib.load(f)["package"]
            versions = [p.get("version") for p in packages if isinstance(p, dict) and p.get("name") == "jsonschema"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("uv.lock jsonschema version unavailable or invalid") from exc
        if len(versions) != 1 or not isinstance(versions[0], str) or not re.fullmatch(r"(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?(?:\+[a-z0-9]+(?:[._-][a-z0-9]+)*)?", versions[0]):
            raise ValueError("uv.lock jsonschema version unavailable or invalid")
        return versions[0]

    def _vendor_claude(self) -> bool:
        """F-19 の版に合う vendor を置く。版の指定があるのに実体が無ければビルドしない。"""
        dest = self.home / "runtime" / "images" / "agent-cli" / "vendor" / "claude"
        ver_file = self.home / "runtime" / "images" / "agent-cli" / "CLAUDE_VERSION"
        if not ver_file.exists():
            return True
        ver = ver_file.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", ver):
            self.log.event("deploy.vendor_missing", "agent-cli", {"result": "invalid_version"})
            return False
        src = Path(os.path.expanduser("~/.local/share/mise/installs/claude")) / ver / "claude"
        if not src.is_file():
            self.log.event("deploy.vendor_missing", "agent-cli", {"result": "missing", "version": ver})
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return True

    def _read_pending(self) -> dict | None:
        path = self.deploy_dir / "pending.json"
        if not path.exists():
            return None
        plan = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict) or plan.get("status") not in ("pending", "completed"):
            raise ValueError("invalid deployment plan")
        if plan["status"] == "completed":
            return None
        if not (isinstance(plan.get("base_sha"), str) and re.fullmatch(r"[0-9a-f]{40}", plan["base_sha"])
                and isinstance(plan.get("base_tag"), str) and plan["base_tag"]
                and isinstance(plan.get("base_config"), dict) and plan["base_config"]
                and all(isinstance(v, dict) for v in plan["base_config"].values())
                and isinstance(plan.get("target"), str) and re.fullmatch(r"[0-9a-f]{40}", plan["target"])):
            raise ValueError("incomplete deployment plan")
        return plan

    def _write_pending(self, plan: dict) -> None:
        write_atomic(self.deploy_dir / "pending.json", json.dumps(plan) + "\n")

    # --- 本体 ---
    def execute(self) -> int:
        (self.cfg.state_dir / "locks").mkdir(parents=True, exist_ok=True)
        lockf = open(self.cfg.state_dir / "locks" / "deploy.lock", "a+")
        try:
            try:
                fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.log.event("deploy.busy", "deploy", {"result": "busy"})
                return EXIT_BUSY
            return self._execute()
        finally:
            lockf.close()

    def _execute(self) -> int:
        old_head = self.git("rev-parse", "HEAD")
        if not old_head.ok:
            self.log.event("deploy.failed", "deploy", {"step": "rev-parse", "result": f"failed:{old_head.rc}"})
            return EXIT_FAILED
        old_head_sha = old_head.out.strip()
        f = self.git("fetch", "local", "main", "refs/tags/accepted/*:refs/tags/accepted/*")
        if not f.ok:
            self.log.event("deploy.failed", "deploy", {"step": "fetch", "result": f"failed:{f.rc}"})
            return EXIT_FAILED
        fh = self.git("rev-parse", "FETCH_HEAD")
        if not fh.ok:
            self.log.event("deploy.failed", "deploy", {"step": "fetch_head", "result": f"failed:{fh.rc}"})
            return EXIT_FAILED
        target = fh.out.strip()
        tags = self.git("tag", "--points-at", target)
        if not (tags.ok and accepted_tag_for(tags.out.splitlines(), target)):
            self.log.event("deploy.rejected", "deploy", {"sha": target[:12], "reason": "no_accepted_tag", "result": "rejected"})
            return EXIT_REJECTED
        try:
            plan = self._read_pending()
        except (OSError, ValueError):
            self.log.event("deploy.failed", "deploy", {"step": "pending", "result": "invalid"})
            return EXIT_FAILED
        cur = self.deploy_dir / "current.env"
        current_tag = self._read_tag(cur)
        if not current_tag and not cur.exists() and target == old_head_sha:
            self._write_current(target[:12])
            current_tag = target[:12]
        if target == old_head_sha and current_tag == target[:12] and plan is None:
            config = self.run(self.cfg.compose("config", "--format", "json", profiles=("build",)),
                              timeout=self.cfg.compose_timeout_s)
            try:
                services = json.loads(config.out)["services"] if config.ok else {}
                images = {service["image"] for service in services.values()
                          if isinstance(service, dict) and service.get("build") and service.get("image")}
            except (ValueError, KeyError, TypeError, AttributeError):
                images = set()
            if images:
                images.add(f"ax/core:{current_tag}")
            if not images:
                self.log.event("deploy.failed", "deploy", {"step": "build_images", "result": "unavailable"})
                return EXIT_FAILED
            if all(self.run([*self.cfg.sudo, "docker", "image", "inspect", image],
                            timeout=self.cfg.compose_timeout_s).ok for image in sorted(images)):
                self.log.event("deploy.completed", "deploy", {"tag": target[:12], "changed": "false", "result": "ok"})
                return EXIT_OK
        if not current_tag:
            self.log.event("deploy.failed", "deploy", {"step": "current_tag", "result": "missing"})
            return EXIT_FAILED
        if plan is None:
            old_conf = self._config_json()
            if old_conf is None:
                self.log.event("deploy.failed", "deploy", {"step": "old_config", "result": "unavailable"})
                return EXIT_FAILED
            plan = {"status": "pending", "base_sha": old_head_sha, "base_tag": current_tag,
                    "base_config": old_conf, "target": target}
        else:
            # 失敗前のサービス定義とタグを保ち、同じ HEAD でもビルドと healthz をやり直す。
            old_conf = plan["base_config"]
        old_tag = plan["base_tag"]
        plan["target"] = target
        self._write_pending(plan)
        if target != old_head_sha:
            m = self.git("merge", "--ff-only", "FETCH_HEAD")
            if not m.ok:
                self.log.event("deploy.failed", "deploy", {"step": "merge", "result": f"failed:{m.rc}"})
                return EXIT_FAILED
        preflight = self.run(self.cfg.compose("ax-ipam-check"), timeout=self.cfg.compose_timeout_s)
        if not preflight.ok:
            self.log.event("deploy.failed", "deploy", {"step": "network_check", "result": f"failed:{preflight.rc}"})
            return EXIT_FAILED
        if current_tag == old_tag:
            shutil.copy2(cur, self.deploy_dir / "previous.env")
        elif not (self.deploy_dir / "previous.env").is_file():
            self.log.event("deploy.failed", "deploy", {"step": "previous_tag", "result": "missing"})
            return EXIT_FAILED
        new_tag = target[:12]
        self._write_current(new_tag)
        if not self._vendor_claude():
            self._restore_previous()
            self.log.event("deploy.rejected", "deploy", {"reason": "vendor_missing", "result": "rejected"})
            return EXIT_REJECTED
        try:
            jsonschema_version = self._jsonschema_version()
        except ValueError as exc:
            self._restore_previous()
            self.log.event("deploy.build_failed", "deploy", {"tag": new_tag, "result": str(exc)})
            return EXIT_FAILED
        b = self.run(self.cfg.compose("build", "--build-arg", f"JSONSCHEMA_VERSION={jsonschema_version}",
                                      profiles=("build",)), timeout=self.cfg.deploy_timeout_s)
        if not b.ok:
            self._restore_previous()
            self.log.event("deploy.build_failed", "deploy", {"tag": new_tag, "result": f"failed:{b.rc}"})
            return EXIT_FAILED
        # The runtime build uses Dockerfile.core; core services use a separate tag.
        core_tag = self.run([*self.cfg.sudo, "docker", "image", "tag",
                             f"ax/runtime:{new_tag}", f"ax/core:{new_tag}"],
                            timeout=self.cfg.compose_timeout_s)
        if not core_tag.ok:
            self._restore_previous()
            self.log.event("deploy.build_failed", "deploy", {"tag": new_tag, "result": f"core_tag_failed:{core_tag.rc}"})
            return EXIT_FAILED
        new_conf = self._config_json()
        if new_conf is None:
            self._restore_previous()
            self.log.event("deploy.failed", "deploy", {"step": "new_config", "result": "unavailable"})
            return EXIT_FAILED
        diff = self.git("diff", "--name-only", f"{plan['base_sha']}..{target}")
        if not diff.ok:
            self._restore_previous()
            self.log.event("deploy.failed", "deploy", {"step": "diff", "result": f"failed:{diff.rc}"})
            return EXIT_FAILED
        paths = diff.out.splitlines()
        reg = {p.name for p in self.cfg.registered_dir.iterdir() if p.is_file() and not p.is_symlink()} if self.cfg.registered_dir.is_dir() else set()
        order, fresh = changed_services(old_conf, new_conf, old_tag, new_tag, paths, reg)
        order = [s for s in order if not self.cfg.is_commander(s)]
        started: list[str] = []
        failed_sub: list[str] = []
        for svc in order:
            r = self._up(svc)
            if r.ok:
                started.append(svc)
                continue
            self.log.event("deploy.service_failed", svc, {"service": svc, "tag": new_tag, "result": f"failed:{r.rc}"})
            if is_core(svc):
                return self._roll_back(svc, old_tag, new_tag, started)
            failed_sub.append(svc)
        if not failed_sub:
            self._write_pending({**plan, "status": "completed"})
        self.log.event("deploy.completed", "deploy", {"tag": new_tag, "started": ",".join(started), "failed": ",".join(failed_sub),
                                                      "result": "partial" if failed_sub else "ok"})
        return EXIT_PARTIAL if failed_sub else EXIT_OK

    def _roll_back(self, svc: str, old_tag: str, new_tag: str, started: list[str]) -> int:
        """変わった中核のサービスが通らない: previous.env の AX_TAG で同じサービスを起動し直す（F-18.3 の (f)）。新しいイメージは消さない。"""
        self._restore_previous()
        r = self._up(svc)
        self.log.event("deploy.rolled_back", svc, {"service": svc, "from_tag": new_tag, "to_tag": old_tag,
                                                    "result": "ok" if r.ok else f"failed:{r.rc}", "started": ",".join(started)})
        return EXIT_ROLLED_BACK


def main(cfg: HostConfig, runner=None, log: HostLog | None = None) -> int:
    return Deploy(cfg, runner, log).execute()
