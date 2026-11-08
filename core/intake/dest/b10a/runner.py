"""緊急ボタンの実行（S-12.3、D-11.3、T-15）。`emergency` サービス（ax-core）の中で動く。

3 段（範囲 scope は all、commanders、プラグインの ID）:
  1 記録   時系列記録の export、生きている命令、台帳、worker-pool のジョブの一覧を 1 つのスナップショットにまとめ、
           退避先（自分のボリュームの archive/emergency-<時刻>/。archiver が運ぶ。F-20）に置く。`emergency.snapshot` を書く。
  2 リセット 範囲の中の、issued_by が manager / backup / inspection の命令をすべて解除する（commanders は commander の命令）。
           範囲の中のコンテナを起動し直す。要求ファイル（S-11.6）だけを書く。ホスト見張りが `sudo -n` で行う。
           `sudo -n` が使えない間は起動し直しだけ行わず、命令の解除は行う。`emergency.reset` を書く。
  3 再開   範囲のプラグインが manifest の既定の mode に戻ったかを読んで記録する（命令を消した結果として戻る。D-11.3）。
           `issued_by=user` の命令は解除しないので、残っていればそのプラグインは既定に戻らない（held_by に出す）。`emergency.resumed` を書く。
外へ済ませた操作（push、コメント）は取り消さない。ここに外部へ出る呼び出しは無い。

どの段も、1 つの失敗で他を止めない（fail-soft）。記録が欠けても、リセットは行う（緊急は止める向きの操作）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from axcommon import ids, schemas, timeutil
from axcommon.client import ApiClient, ApiError, LedgerClient, TimelineClient, Unavailable
from axcommon.events import EventSink

from core.observer import hostreq

log = logging.getLogger("emergency")

RESET_ISSUERS = ("manager", "backup", "inspection")
COMMANDER_ISSUERS = ("commander",)
COMMAND_STATES = ("active", "expired_pending")



class EmergencyError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code


@dataclass
class EmergencyConfig:
    host: str = "unknown"
    data_dir: str = "/data"
    state_dir: str = "/state"

    @property
    def archive_dir(self) -> Path:
        return Path(self.data_dir) / "archive"

    @property
    def requests_dir(self) -> Path:
        return Path(self.state_dir) / "host-requests"


class SudoProbe:
    """ホスト見張りのログ（/state/hostwatch.log）の最後の host.sudo_* から、`sudo -n` が使えるかを読む。
    ログが無い・読めない・該当が無ければ None（不明。起動し直しの要求は書く。ホスト見張りが自分で判断する）。"""

    def __init__(self, state_dir: str | os.PathLike):
        self.path = Path(state_dir) / "hostwatch.log"

    def available(self) -> bool | None:
        try:
            size = self.path.stat().st_size
            with open(self.path, "rb") as f:
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            return None
        state: bool | None = None
        for line in tail.splitlines():
            try:
                kind = json.loads(line).get("kind")
            except (ValueError, AttributeError):
                continue
            if kind == "host.sudo_unavailable":
                state = False
            elif kind == "host.sudo_restored":
                state = True
        return state


def _unavailable_redactor(x: Any) -> Any:
    raise RuntimeError("snapshot_redactor_unavailable")


class EmergencyRunner:
    def __init__(self, cfg: EmergencyConfig, *, timeline: TimelineClient, control: ApiClient, ledger: LedgerClient,
                 pool: ApiClient | None, sink: EventSink | None, sudo: Callable[[], bool | None] | None = None,
                 redact: Callable[[Any], Any] | None = None, now: Callable[[], Any] | None = None):
        self.cfg, self.timeline, self.control, self.ledger, self.pool, self.sink = cfg, timeline, control, ledger, pool, sink
        self._sudo = sudo or SudoProbe(cfg.state_dir).available
        if redact is None:
            try:
                from axcommon.redact import from_environment

                redact = from_environment().as_hook()
            except Exception:  # 検査できない記録だけを止め、命令の解除は続ける。
                redact = _unavailable_redactor
        self._redact = redact
        self._now = now or timeutil.now
        self._lock = threading.Lock()
        self._runs_path = Path(cfg.data_dir) / "runs.jsonl"

    # --- 事象 ---
    def _emit(self, kind: str, subject: str, data: dict) -> None:
        if self.sink is None:
            return
        try:
            self.sink.emit(kind, subject, data)
        except Exception:  # noqa: BLE001  記録の失敗で緊急の手順を止めない（S-2.2）
            log.error("%s を書けない", kind)

    # --- 冪等（同じ request_id は同じ結果） ---
    def _previous(self, request_id: str | None) -> dict | None:
        if not request_id:
            return None
        try:
            for line in self._runs_path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("request_id") == request_id:
                    return row.get("result")
        except OSError:
            return None
        return None

    def _remember(self, request_id: str | None, result: dict) -> None:
        if not request_id:
            return
        try:
            self._runs_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._runs_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"request_id": request_id, "at": timeutil.format_ts(self._now()), "result": result},
                                   ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            log.error("実行の記録を書けない")

    # --- 範囲 ---
    def plugin_ids(self) -> list[str] | None:
        try:
            resp = self.control.get("/v1/plugins")
        except (Unavailable, ApiError):
            return None
        items = (resp or {}).get("plugins") if isinstance(resp, dict) else resp
        return [str(p["id"]) for p in (items or []) if isinstance(p, dict) and p.get("id")]

    def check_scope(self, scope: str) -> list[str] | None:
        """範囲を確かめる。プラグインの ID なら登録に在ることを確かめる（制御 API が見えなければ通す。緊急は止める向き）。"""
        if scope in ("all", "commanders"):
            return None
        plugins = self.plugin_ids()
        if plugins is not None and scope not in plugins:
            raise EmergencyError(404, "unknown_scope", "登録にないプラグイン")
        return plugins

    # --- 本体 ---
    def run(self, scope: str, request_id: str | None = None) -> dict:
        if schemas.errors("ext/b10a/emergency.payload.schema.json", {"scope": scope}):
            raise EmergencyError(422, "bad_request", "scope の形が違う")
        if not self._lock.acquire(blocking=False):
            raise EmergencyError(409, "busy", "緊急の手順が実行中")
        try:
            prev = self._previous(request_id)
            if prev is not None:
                if prev.get("scope") != scope:
                    raise EmergencyError(409, "idempotency_mismatch", "同じ request_id の範囲が違う")
                return {**prev, "replayed": True}
            plugins = self.check_scope(scope)
            ts = timeutil.format_ts(self._now())
            snapshot = self._snapshot(scope, ts)
            reset = self._reset(scope, snapshot)
            resume = self._resume(scope, plugins, snapshot)
            result = {"ok": True, "scope": scope, "request_id": request_id, "snapshot": snapshot, "reset": reset, "resume": resume,
                      "replayed": False}
            self._remember(request_id, result)
            return result
        finally:
            self._lock.release()

    def _timeline_snapshot(self) -> dict:
        exported = self.timeline.post("/v1/export", {"days": 0})
        upper = exported.get("last_seq") if isinstance(exported, dict) else None
        if not isinstance(upper, int) or isinstance(upper, bool) or upper < 0:
            raise ValueError("timeline_export_cursor_missing")
        events = []
        after = 0
        while after < upper:
            page = self.timeline.get_events(after=after, limit=min(200, upper - after))
            batch = (page or {}).get("events", [])
            bounded = [row for row in batch if isinstance(row, dict) and isinstance(row.get("seq"), int)
                       and after < row["seq"] <= upper]
            if not bounded:
                raise ValueError("timeline_snapshot_incomplete")
            events.extend(bounded)
            after = max(row["seq"] for row in bounded)
        return {**exported, "events": events}

    # 1 記録
    def _snapshot(self, scope: str, ts: str) -> dict:
        name = "emergency-" + ts.replace("-", "").replace(":", "").replace(".", "")[:-1] + "-" + ids.new_ulid()
        d = self.cfg.archive_dir / name
        sections: dict[str, dict] = {}

        def section(key: str, fn: Callable[[], Any]) -> None:
            try:
                data = fn()
                self._write(d / f"{key}.json", data)
                sections[key] = {"ok": True, "count": self._count(data)}
            except (Unavailable, ApiError) as e:
                sections[key] = {"ok": False, "error": type(e).__name__}
            except OSError as e:
                sections[key] = {"ok": False, "error": f"write:{type(e).__name__}"}
            except Exception as e:  # noqa: BLE001
                sections[key] = {"ok": False, "error": type(e).__name__}

        section("timeline-export", self._timeline_snapshot)
        section("commands", lambda: {s: self.control.get("/v1/commands", params={"state": s}) for s in COMMAND_STATES})
        section("todos", lambda: self.ledger.list_todos())
        if self.pool is not None:
            section("jobs", lambda: self.pool.get("/v1/jobs"))
        else:
            sections["jobs"] = {"ok": False, "error": "no_pool_client"}
        complete = all(v["ok"] for v in sections.values())
        path: str | None = f"archive/{name}"
        try:
            self._write(d / "manifest.json", {"scope": scope, "at": ts, "sections": sections, "complete": complete})
        except Exception:  # 記録先や秘密検査の故障は、リセットへ伝播させない。
            complete = False
            if not any(v["ok"] for v in sections.values()):
                path = None
        self._emit("emergency.snapshot", scope, {"scope": scope, "snapshot_path": path, "complete": complete, "sections": sections})
        return {"path": path, "complete": complete, "sections": sections}

    @staticmethod
    def _count(data: Any) -> int | None:
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            for k in ("todos", "commands", "jobs", "events"):
                if isinstance(data.get(k), list):
                    return len(data[k])
        return None

    def _write(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("." + path.name + ".tmp")
        tmp.write_text(json.dumps(self._redact(data), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    # 2 リセット
    def _commands(self, issuer: str) -> list[dict]:
        out: list[dict] = []
        for state in COMMAND_STATES:
            resp = self.control.get("/v1/commands", params={"state": state, "issued_by": issuer})
            items = (resp or {}).get("commands") if isinstance(resp, dict) else resp
            out.extend(c for c in (items or []) if isinstance(c, dict) and c.get("id")
                       and c.get("issued_by") == issuer and c.get("state") == state)
        return out

    @staticmethod
    def _in_scope(cmd: dict, scope: str) -> bool:
        return scope in ("all", "commanders") or cmd.get("target") == scope

    def _reset(self, scope: str, snapshot: dict) -> dict:
        issuers = COMMANDER_ISSUERS if scope == "commanders" else RESET_ISSUERS
        released: list[str] = []
        failed: list[str] = []
        kept: dict[str, int] = {}
        listing_failed = False
        for issuer in sorted({*RESET_ISSUERS, *COMMANDER_ISSUERS, "user"}):
            try:
                cmds = self._commands(issuer)
            except (Unavailable, ApiError):
                listing_failed = True
                continue
            for c in cmds:
                if not self._in_scope(c, scope):
                    continue
                if issuer not in issuers:
                    kept[issuer] = kept.get(issuer, 0) + 1   # user の命令は解除しない（解除は利用者の指示）
                    continue
                try:
                    self.control.post(f"/v1/commands/{c['id']}/release", {"reason": "emergency"})
                    released.append(c["id"])
                except (Unavailable, ApiError):
                    failed.append(c["id"])
        restart = self._restart(scope)
        reset = {"released": released, "release_failed": failed, "kept": kept, "restart": restart, "listing_failed": listing_failed}
        self._emit("emergency.reset", scope, {"scope": scope, "released": len(released), "release_failed": len(failed), "kept": kept,
                                              "restart": restart})
        return reset

    def _registered(self) -> list[str]:
        d = Path(self.cfg.state_dir) / "hostwatch" / "registered"
        try:
            return sorted(p.name for p in d.iterdir() if p.is_file())
        except OSError:
            return []

    def services_for(self, scope: str) -> list[str]:
        if scope == "commanders":
            return []
        registered = self._registered()
        if scope == "all":
            return registered
        return [s for s in (f"sl-{scope}", scope) if s in registered]

    def _restart(self, scope: str) -> dict:
        if scope == "commanders":
            return {"requested": [], "skipped": "not_applicable"}
        services = self.services_for(scope)
        if not services:
            return {"requested": [], "skipped": "no_services"}
        if self._sudo() is False:
            return {"requested": [], "skipped": "sudo_unavailable"}   # 起動し直しだけ行わない（命令の解除は済んでいる）
        requested = []
        have = set(hostreq.pending(self.cfg.requests_dir))
        for s in services:
            if ("restart", s) in have:
                requested.append(s)
                continue
            try:
                hostreq.write_request(self.cfg.requests_dir, "restart", s)
                requested.append(s)
            except (OSError, ValueError):
                log.error("起動し直しの要求を書けない")
        return {"requested": requested, "skipped": None if requested else "request_write_failed"}

    # 3 再開
    @staticmethod
    def _default_mode(view: dict) -> str | None:
        caps = ((view.get("manifest") or {}).get("capabilities")) or []
        if "run" in caps:
            return "run"
        return caps[0] if caps else None

    def _mode_holders(self, plugin_id: str) -> list[str]:
        """今も mode を変えている命令を出した者（解除しなかった user など）。読めなければ unknown。"""
        try:
            resp = self.control.get("/v1/commands", params={"state": "active"})
        except (Unavailable, ApiError):
            return ["unknown"]
        items = (resp or {}).get("commands") if isinstance(resp, dict) else resp
        return sorted({str(c.get("issued_by")) for c in (items or []) if isinstance(c, dict) and c.get("op") == "set_mode"
                       and c.get("target") in (plugin_id, "all")}) or ["unknown"]

    def _resume(self, scope: str, plugins: list[str] | None, snapshot: dict) -> dict:
        if scope == "commanders":
            self._emit("emergency.resumed", scope, {"scope": scope, "plugins": {}, "snapshot_path": snapshot["path"]})
            return {"plugins": {}, "unavailable": False}
        if plugins is None:
            plugins = self.plugin_ids()
        targets = [scope] if scope != "all" else (plugins or [])
        rows: dict[str, dict] = {}
        unavailable = plugins is None
        for pid in targets:
            try:
                view = self.control.get(f"/v1/plugins/{pid}")
                eff = self.control.get(f"/v1/plugins/{pid}/effective")
            except (Unavailable, ApiError):
                rows[pid] = {"error": "unavailable"}
                unavailable = True
                continue
            default = self._default_mode(view or {})
            mode = (eff or {}).get("mode")
            row: dict[str, Any] = {"mode": mode, "default_mode": default, "at_default": mode == default}
            if mode != default:
                row["held_by"] = self._mode_holders(pid)
            rows[pid] = row
        self._emit("emergency.resumed", scope, {"scope": scope, "plugins": rows, "snapshot_path": snapshot["path"]})
        return {"plugins": rows, "unavailable": unavailable}
