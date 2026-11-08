"""ホスト見張りの 1 周期（S-11.5、D-10.3）。順序は D-10.3 の 1〜6。

1 sudo -n true の事前確認。失敗なら特権操作（2、3、5）だけを飛ばし、host.sudo_unavailable を書く（遷移のときだけ）。戻ったら host.sudo_restored。
2 compose ps（--all）で、登録済みのサービスのうち exited・dead・created・unhealthy・無い物を起動し直す。
3 observer.beat が observer_stale_s より古ければ observer を起動し直す。
4 対象は hostwatch/registered/<名前> の積集合だけ。commander_unit に当たる名前は除く。登録のない部品は起動しない。
5 host-requests/ の要求を読んで実行する（restart、deploy、seat-sync）。
6 行ったこと（操作名、対象、結果だけ）を hostwatch.log と journal に書く。
索引の再作成は sudo を使い、失敗しても他の仕事を止めない。同じ周期の重なりは flock -n で飛ばす。
"""
from __future__ import annotations

import fcntl
import os
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from axcommon import timeutil

from . import compose as cx
from . import requests as rq
from .config import HostConfig
from .hostlog import HostLog
from .runner import Result, SubprocessRunner

OBSERVER = "observer"
RESTART_COOLDOWN_NOTE = "observer-restarted-at"


@contextmanager
def cycle_lock(cfg: HostConfig):
    d = cfg.state_dir / "locks"
    d.mkdir(parents=True, exist_ok=True)
    f = open(d / "hostwatch.lock", "a+")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        f.close()


class HostWatch:
    def __init__(self, cfg: HostConfig, runner=None, log: HostLog | None = None, *, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.run = (runner or SubprocessRunner()).run
        self.log = log or HostLog(cfg.log_path, cfg.host)
        self.clock = clock
        self.sudo_ok = True
        self._ps: dict[str, dict] | None = None
        self._ps_loaded = False
        self._services: list[str] | None = None

    # --- 補助 ---
    def _state_file(self, name: str) -> Path:
        return self.cfg.state_dir / "hostwatch" / name

    def registered(self) -> list[str]:
        try:
            names = [p.name for p in self.cfg.registered_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
        except OSError:
            return []
        return sorted(n for n in names if not self.cfg.is_commander(n))

    def compose_ps(self) -> dict[str, dict] | None:
        """compose ps の結果。取れなかったとき None（状態が分からない間は、何も起動し直さない）。"""
        if not self._ps_loaded:
            r = self.run(self.cfg.compose("ps", "--all", "--format", "json"), timeout=self.cfg.compose_timeout_s)
            try:
                self._ps = cx.parse_ps(r.out) if r.ok else None
            except (ValueError, TypeError):
                self._ps = None
            self._ps_loaded = True
            if self._ps is None:
                self.log.event("host.action", "compose-ps", {"op": "ps", "target": "compose", "result": f"failed:{r.rc}"})
        return self._ps

    def compose_services(self) -> list[str]:
        if self._services is None:
            r = self.run(self.cfg.compose("config", "--services"), timeout=self.cfg.compose_timeout_s)
            self._services = cx.parse_services(r.out) if r.ok else []
        return self._services

    def beat_age_s(self) -> float | None:
        """/state/observer.beat の古さ（秒）。中身の時刻を読み、読めなければ更新時刻。無ければ None。"""
        p = self.cfg.state_dir / "observer.beat"
        try:
            text = p.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            return self.clock() - timeutil.parse_ts(text).timestamp()
        except ValueError:
            try:
                return self.clock() - p.stat().st_mtime
            except OSError:
                return None

    # --- 特権操作（sudo -n 経由。Runner が実行する） ---
    def start_service(self, svc: str, reason: str) -> bool:
        """止まっている・無い物は up -d、動いているが不調な物は restart。結果を記録する。"""
        row = (self.compose_ps() or {}).get(svc)
        if row is not None and row["state"] == "running":
            cmd = self.cfg.compose("restart", svc)
            op = "restart"
        else:
            cmd = self.cfg.compose("up", "--no-deps", "-d", svc)
            op = "up"
        r = self.run(cmd, timeout=self.cfg.compose_timeout_s)
        self.log.event("host.action", f"{op}:{svc}", {"op": op, "target": svc, "result": "ok" if r.ok else f"failed:{r.rc}", "reason": reason})
        if svc == OBSERVER:
            self._touch_cooldown()
        return r.ok

    def _iso_now(self) -> str:
        return timeutil.format_ts(datetime.fromtimestamp(self.clock(), tz=timezone.utc))

    def _marker_age_s(self, name: str) -> float | None:
        """印のファイルの古さ（秒）。中身の時刻を読み、読めなければ更新時刻。無ければ None。"""
        p = self._state_file(name)
        try:
            text = p.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            return self.clock() - timeutil.parse_ts(text).timestamp()
        except ValueError:
            try:
                return self.clock() - p.stat().st_mtime
            except OSError:
                return None

    def _touch_cooldown(self) -> None:
        self._write_marker(self._state_file(RESTART_COOLDOWN_NOTE), self._iso_now())

    def _in_cooldown(self) -> bool:
        """observer を起動し直した直後は、beat が戻るまで observer_stale_s だけ待つ（起動し直しの連打を避ける）。"""
        age = self._marker_age_s(RESTART_COOLDOWN_NOTE)
        return age is not None and age < self.cfg.observer_stale_s

    # --- 1 周期 ---
    def cycle(self) -> dict:
        res = {"sudo": None, "started": [], "requests": [], "index": None}
        self.sudo_ok = self._check_sudo()
        res["sudo"] = self.sudo_ok
        if self.sudo_ok:
            services = self.compose_services()
            # 登録のうち compose のサービスに当たる物だけ（ホスト見張り自身の印のように compose に無い名前は起動の対象にしない）
            reg = [n for n in self.registered() if n in services]
            handled: set[str] = set()
            # 監視を先に（登録の順は ax-up.sh が守る）。observer の beat が古ければ先に直す
            age = self.beat_age_s()
            stale = OBSERVER in reg and (age is None or age > self.cfg.observer_stale_s) and not self._in_cooldown()
            ps = self.compose_ps()
            order = sorted(reg, key=lambda n: (n != OBSERVER, n)) if ps is not None else []
            for svc in order:
                row = ps.get(svc)
                if svc == OBSERVER and stale and not cx.needs_up(row):
                    if self.start_service(svc, "observer.beat が古い"):
                        res["started"].append(svc)
                    handled.add(svc)
                elif cx.needs_up(row):
                    if svc == OBSERVER and self._in_cooldown():
                        continue
                    if self.start_service(svc, "状態が不調" if row else "コンテナが無い"):
                        res["started"].append(svc)
                    handled.add(svc)
            res["requests"] = self.process_requests(reg, handled)
        res["index"] = self.rebuild_index_if_due()
        return res

    def _check_sudo(self) -> bool:
        r = self.run([*self.cfg.sudo, "true"], timeout=30)
        marker = self._state_file("sudo.state")
        try:
            prev = marker.read_text().strip()
        except (OSError, UnicodeError):
            prev = "ok"
        if r.ok:
            if prev == "unavailable":
                self.log.event("host.sudo_restored", "sudo", {"result": "ok"})
            self._write_marker(marker, "ok")
            return True
        if prev != "unavailable":
            self.log.event("host.sudo_unavailable", "sudo", {"result": f"failed:{r.rc}"})
        self._write_marker(marker, "unavailable")
        return False

    @staticmethod
    def _write_marker(p: Path, text: str) -> None:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text + "\n", encoding="utf-8")
        except OSError:
            pass

    # --- 要求 ---
    def process_requests(self, reg: list[str], handled: set[str]) -> list[tuple[str, str]]:
        d = self.cfg.requests_dir
        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str | None]] = set()
        for rq_ in rq.read_requests(d, self.cfg.max_requests_per_cycle):
            if rq_.error is not None:
                self.log.event("host.action", "request", {"op": "request", "target": rq_.path.name[:40], "result": "rejected", "reason": rq_.error})
                rq.archive(rq_, d, "rejected")
                out.append((rq_.path.name, "rejected"))
                continue
            key = (rq_.op, rq_.name)
            if key in seen:  # 同じ要求は 1 回だけ実行する
                rq.archive(rq_, d, "done")
                out.append((rq_.path.name, "merged"))
                continue
            result = self._execute(rq_, reg, handled)
            if result != "deferred":
                seen.add(key)
                rq.archive(rq_, d, "rejected" if result == "rejected" else "done")
            out.append((rq_.path.name, result))
        return out

    def _execute(self, r: rq.Request, reg: list[str], handled: set[str]) -> str:
        if r.op == "restart":
            name = r.name
            if name is None or name not in reg or name not in self.compose_services() or self.cfg.is_commander(name):
                self.log.event("host.action", f"restart:{name}", {"op": "restart", "target": str(name), "result": "rejected"})
                return "rejected"
            if self.compose_ps() is None:
                self.log.event("host.action", f"restart:{name}", {"op": "restart", "target": name, "result": "deferred"})
                return "deferred"
            if name in handled:
                self.log.event("host.action", f"restart:{name}", {"op": "restart", "target": name, "result": "skipped", "reason": "この周期で起動し直し済み"})
                return "skipped"
            ok = self.start_service(name, "要求")
            handled.add(name)
            return "ok" if ok else "failed"
        if r.op == "deploy":
            return self._run_script("deploy", ["bash", str(self.cfg.repo / "host" / "ax-deploy.sh")], self.cfg.deploy_timeout_s)
        if r.op == "seat-sync":
            script = self.cfg.repo / "host" / "ax-seat.sh"
            if not script.exists():
                self.log.event("host.action", "seat-sync", {"op": "seat-sync", "target": "seat", "result": "unavailable", "reason": "host/ax-seat.sh が無い"})
                return "unavailable"
            return self._run_script("seat-sync", ["bash", str(script), "sync"], 300)
        return "rejected"

    def _run_script(self, op: str, argv: list[str], timeout: float) -> str:
        env = {**os.environ, "AX_HOME": str(self.cfg.ax_home), "AX_PROFILE": str(self.cfg.profile_path),
               "AX_HOSTWATCH_SUDO": shlex.join(self.cfg.sudo), "AX_UP_SUDO": shlex.join(self.cfg.sudo)}
        r = self.run(argv, timeout=timeout, env=env)
        result = "ok" if r.ok else ("rejected" if (op == "deploy" and r.rc == 10) else f"failed:{r.rc}")
        self.log.event("host.action", op, {"op": op, "target": op, "result": result})
        return "rejected" if result == "rejected" else ("ok" if r.ok else "failed")

    # --- 索引の再作成（起動時と 1 時間ごと。F-18.6 の B8 の仕上げ） ---
    def rebuild_index_if_due(self) -> str | None:
        marker = self._state_file("index.last")
        age = self._marker_age_s("index.last")
        if age is not None and age < self.cfg.index_rebuild_s:
            return None
        r = self.run(["bash", str(self.cfg.repo / "host" / "ax-redact-index.sh"), "--profile", str(self.cfg.profile_path)],
                     timeout=300, env={**os.environ, "AX_REDACT_SUDO": shlex.join(self.cfg.sudo)})
        if r.ok:
            self._write_marker(marker, self._iso_now())
            self.log.event("host.action", "redact-index", {"op": "redact-index", "target": "index", "result": "ok"})
            return "ok"
        # 失敗: 索引は古いまま（redact は他の基準だけで動く。S-15.3）。health.alert はスクリプト自身が書く。次の周期でやり直す
        self.log.event("host.action", "redact-index", {"op": "redact-index", "target": "index", "result": f"failed:{r.rc}"})
        return "failed"


def run_cycle(cfg: HostConfig, runner=None, log=None, clock=time.time) -> dict | None:
    with cycle_lock(cfg) as got:
        if not got:
            return None
        return HostWatch(cfg, runner, log, clock=clock).cycle()
