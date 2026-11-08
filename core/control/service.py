"""制御 API の業務のまとめ（S-5、D-4）。

- 登録（D-4.1）: 起動時に registry.yaml から読み、manifest を検査する。revision は登録・命令・解除のたびに増やす。
- 命令（D-4.2）: 検証 S-5.4 の 1〜10 → active で保存して control.issued → 有効な値を計算（S-5.5）→
  対象の POST /apply を呼ぶ（失敗しても対象が control_poll_s ごとに effective を読むので届く）。
- 期限（D-4.3）: 期限を過ぎた active な命令は、on_expiry=release なら解除（control.released by=expiry）、
  ask_issuer なら expired_pending にして効力を残す（S-5.6）。延長は renewals < control_max_renewals。
- タイマー（D-4.4）: POST /v1/timers は件数・寿命・action を検証する。期限側（expires_at と control.release の
  実行）はここで行う。実際の起動（ledger.recheck / plugin.run_now の実行）は timer-runner（B7）が行う。
- 継続許可（S-5.7、S-20.3）: 一覧・照合・作成・無効化。
"""
from __future__ import annotations

import logging
import contextlib
import fcntl
import os
import math
import threading
import time
from functools import wraps
from datetime import timedelta
from typing import Any

from axcommon import ids, schemas, timeutil
from axcommon.client import ApiClient, ApiError, Unavailable
from axcommon.defaults import Defaults
from axcommon.events import EventSink

from . import MODES, OPERATION_KINDS, PRIORITY, SENDING_OUTPUTS, TIMER_ACTIONS, Reject
from .store import Store

log = logging.getLogger("core.control")

CORE_COMPONENTS = {"control", "timeline", "ledger", "resources", "intake", "manager", "observer", "backup",
                   "inspector", "hq", "memory-connector", "archiver", "hostwatch", "gate", "emergency"}

COMMAND_STATES = ("active", "expired_pending", "released")
TIMER_STATES = ("active", "fired", "released", "expired")

# S-5.3 の命令の形（schemas/control-command.schema.json の項目。保存側の state などは含まない）
COMMAND_FIELDS = ("id", "target", "op", "args", "reason", "issued_by", "issued_at", "expires_at", "on_expiry", "corr")


def clean_command(c: dict) -> dict:
    """保存側の項目（state、renewals、released_*）を除いた、S-5.3 の形の命令。"""
    out = {k: c[k] for k in COMMAND_FIELDS if k in c}
    out.setdefault("args", c.get("args") or {})
    return out


def serialized(fn):
    @wraps(fn)
    def call(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return call


def parse_time(value, field: str):
    try:
        return timeutil.parse_ts(value)
    except (TypeError, ValueError):
        raise Reject(422, "bad_request", f"{field} は RFC 3339 の時刻が必須") from None


def knob_check(spec: dict, value: Any) -> str | None:
    """S-4.3 の調整項目の値の検査。合わなければ人の読める理由を返す。"""
    t = spec.get("type")
    if t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return "integer でない"
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "number でない"
    elif t == "boolean":
        if not isinstance(value, bool):
            return "boolean でない"
    elif t == "enum":
        if not any(value == item and isinstance(value, bool) == isinstance(item, bool) for item in (spec.get("enum") or [])):
            return f"enum の外（{spec.get('enum')}）"
    elif t in ("string", "duration"):
        if not isinstance(value, str):
            return f"{t} でない"
    elif t == "list":
        if not isinstance(value, list):
            return "list でない"
    if isinstance(value, float) and not math.isfinite(value):
        return "有限の数値でない"
    if t in ("integer", "number") and isinstance(value, (int, float)) and not isinstance(value, bool):
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and value < lo:
            return f"min={lo} より小さい"
        if hi is not None and value > hi:
            return f"max={hi} より大きい"
    return None


class ControlService:
    def __init__(self, store: Store, sink: EventSink | None, *, defaults: Defaults | None = None,
                 applier_transport=None, registry_root=None, registry_file=None, resources_client=None, exclusion_adapter=None):
        self._lock = threading.RLock()
        self._limits_lock = threading.RLock()
        self._limits_depth = threading.local()
        self._limits_registry_pending = False
        self.store = store
        self.sink = sink
        self.defaults = defaults or Defaults.load()
        self._applier_transport = applier_transport
        self.resources_client = resources_client
        self._registry_root = registry_root
        self._registry_file = registry_file
        self.graph_check_available: bool | None = None
        self.validate_delivery_scope = None
        # Optional adapter must apply or release by command ID idempotently.
        self.exclusion_adapter = exclusion_adapter

    @contextlib.contextmanager
    def _limits_transaction(self):
        # Serialize a freeze, command commit, and publication across control processes.
        with self._limits_lock:
            depth = getattr(self._limits_depth, "value", 0)
            fd = None
            try:
                if depth == 0 and str(self.store.path) != ":memory:":
                    fd = os.open(str(self.store.path) + ".limits.lock", os.O_CREAT | os.O_RDWR, 0o600)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                self._limits_depth.value = depth + 1
                yield
            finally:
                self._limits_depth.value = depth
                if fd is not None:
                    os.close(fd)

    def _limits_client(self):
        if self.resources_client is not None:
            return self.resources_client
        from axcommon.client import service_url
        from axcommon.creds import read_secret_optional
        token = read_secret_optional("core-token-control")
        if not token:
            raise Unavailable("control credential missing")
        return self._client(service_url("resources"), token)

    def _send_limits(self, models: dict, families: dict, *, frozen: bool = False) -> bool:
        try:
            client = self._limits_client()
            remote = client.get("/v1/slots/effective-limits")
            remote_revision = remote["revision"]
            if type(remote_revision) is not int or remote_revision < 0:
                raise ValueError("invalid remote revision")
            revision = self.store.next_limits_revision(remote_revision)
            client.put("/v1/slots/effective-limits", {"revision": revision,
                       "expires_at": time.time() + 60, "models": models, "families": families})
            self.store.limits_sync_result("frozen" if frozen else "ok", None, timeutil.now_ts())
            return True
        except Exception as exc:  # network, malformed catalog, and storage errors must not stop control
            log.warning("effective limits sync failed: %s", type(exc).__name__)
            try:
                self.store.limits_sync_result("failed", type(exc).__name__, timeutil.now_ts())
            except Exception:
                log.exception("could not record effective limits failure")
            return False

    @serialized
    def sync_limits(self) -> bool:
        try:
            with self._limits_transaction():
                return self._sync_limits_locked()
        except Exception as exc:
            log.warning("effective limits lock failed: %s", type(exc).__name__)
            try:
                self.store.limits_sync_result("failed", type(exc).__name__, timeutil.now_ts())
            except Exception:
                log.exception("could not record effective limits failure")
            return False

    def _sync_limits_locked(self) -> bool:
        try:
            plugin = self.store.get_plugin("worker-pool")
            if not plugin or plugin["status"] != "registered":
                return self._send_limits({}, {})
            knobs = self.effective("worker-pool")["knobs"]
            model_knobs = knobs.get("model_cap") or {}
            family_knobs = knobs.get("family_cap") or {}
            if not isinstance(model_knobs, dict) or not isinstance(family_knobs, dict):
                raise ValueError("invalid cap settings")
            rows = self._limits_client().get("/v1/resources", params={"kind": "model"})["resources"]
            if not isinstance(rows, list) or len(rows) >= 500:
                raise ValueError("incomplete catalog")
            models, families = {}, {}
            free_caps = []
            for row in rows:
                model, family = row["id"], row["family"]
                if not isinstance(model, str) or not model or not isinstance(family, str) or not family:
                    raise ValueError("invalid catalog")
                cap = model_knobs.get(model, row.get("cap", 1))
                if type(cap) is not int or cap < 0:
                    raise ValueError("invalid model cap")
                models[model] = cap
                families[family] = families.get(family, 0) + cap
                if row.get("tier") == "free" and row.get("status") == "active":
                    free_caps.append(row.get("cap") or 0)
            for family, cap in family_knobs.items():
                if family not in families or type(cap) is not int or cap < 0:
                    raise ValueError("invalid family cap")
                families[family] = cap
            if free_caps and "free" in families:
                families["free"] = min(families["free"], sum(free_caps))
            return self._send_limits(models, families)
        except Exception as exc:
            log.warning("effective limits calculation failed: %s", type(exc).__name__)
            try:
                self.store.limits_sync_result("failed", type(exc).__name__, timeutil.now_ts())
            except Exception:
                log.exception("could not record effective limits failure")
            return False

    def _changes_limits(self, command: dict) -> bool:
        return (command["op"] == "set_knob" and command["target"] in ("worker-pool", "all")
                and command["args"].get("name") in ("model_cap", "family_cap"))

    def _freeze_limits(self) -> None:
        if not self._send_limits({}, {}, frozen=True):
            raise Reject(503, "limits_unavailable", "上限を台帳へ反映できない")

    # ---- 事象 ----
    def emit(self, kind: str, subject: str, data: dict, **kw) -> None:
        if self.sink is None:
            return
        try:
            self.sink.emit(kind, subject, data, **kw)
        except Exception:  # noqa: BLE001  記録の失敗で制御を止めない（S-2.2）
            log.exception("事象 %s を送れない", kind)

    def _command_event(self, kind: str, command: dict, extra: dict | None = None) -> None:
        data: dict[str, Any] = {"command": clean_command(command)}
        if extra:
            data.update(extra)
        schemas.validate("event-data", data, fragment=f"/$defs/{kind}")
        self.emit(kind, command["id"], data)

    def _rejected(self, subject: str, body: dict, reason: str) -> None:
        data = {"command": body, "reason": reason}
        self.emit("control.rejected", subject, data)

    # ---- 登録（D-4.1）----
    @serialized
    def register(self) -> dict:
        from .registry import register_all

        # Manifest defaults can lower limits too. Preserve the old worker-pool
        # registration until the resource service has acknowledged a freeze.
        try:
            with self._limits_transaction():
                frozen = self._send_limits({}, {}, frozen=True)
                self._limits_registry_pending = not frozen
                result = register_all(self.store, root=self._registry_root, registry_file=self._registry_file,
                                      preserve_ids=() if frozen else ("worker-pool",))
                if frozen:
                    self.sync_limits()
        except OSError as exc:
            self._limits_registry_pending = True
            try:
                self.store.limits_sync_result("failed", type(exc).__name__, timeutil.now_ts())
            except Exception:
                log.exception("could not record effective limits failure")
            result = register_all(self.store, root=self._registry_root, registry_file=self._registry_file,
                                  preserve_ids=("worker-pool",))
        for plugin_id in result["registered"]:
            p = self.store.get_plugin(plugin_id)
            self.emit("plugin.registered", plugin_id,
                      {"manifest_version": p["manifest"].get("version", ""), "mode": self.effective(plugin_id)["mode"]})
        for plugin_id, errs in result["rejected"]:
            self._rejected(plugin_id, {"target": plugin_id, "op": "register"}, "; ".join(errs))
        from . import graphcheck

        self.graph_check_available = graphcheck.available()
        return result

    def retry_limits_registration(self) -> None:
        if self._limits_registry_pending:
            self.register()

    def plugin_view(self, plugin_id: str) -> dict | None:
        p = self.store.get_plugin(plugin_id)
        if p is None:
            return None
        eff = (self.effective(plugin_id) if p["status"] == "registered" else
               {"revision": p["revision"], "mode": "draining", "knobs": {}})
        return {"id": p["id"], "kind": p["kind"], "status": p["status"], "note": p["note"],
                "revision": eff["revision"], "mode": eff["mode"], "knobs": eff["knobs"],
                "manifest": p["manifest"], "user_set_knobs": p.get("user_set_knobs") or {},
                "registered_at": p["registered_at"]}

    # ---- 有効な値（S-5.5、D-4.2）----
    def effective(self, plugin_id: str) -> dict:
        p = self.store.get_plugin(plugin_id)
        if p is None or p["status"] != "registered" or plugin_id in CORE_COMPONENTS:
            raise Reject(404, "not_found", f"プラグイン {plugin_id} が登録に無い")
        manifest = p["manifest"]
        mode = "collect_only" if set(manifest.get("outputs") or []) & SENDING_OUTPUTS else "run"
        if manifest.get("default_mode") in ("collect_only", "poll_paused", "draining") and manifest["default_mode"] in (manifest.get("capabilities") or []):
            mode = manifest["default_mode"]
        if "run" not in (manifest.get("capabilities") or []) and mode not in (manifest.get("capabilities") or []):
            mode = (manifest.get("capabilities") or ["draining"])[0]
        knobs = {name: spec.get("default") for name, spec in (manifest.get("knobs") or {}).items()}
        class _Newest:
            """発行時刻の新しい方を前にするための比較の鍵（同じ優先順位では新しい命令が勝つ。S-5.5）。"""

            __slots__ = ("ts", "seq")

            def __init__(self, ts: str, seq: int):
                self.ts, self.seq = ts, seq

            def __lt__(self, other: "_Newest") -> bool:
                if self.ts != other.ts:
                    return self.ts > other.ts
                return self.seq > other.seq

        commands = sorted(self.store.living_commands(),
                          key=lambda c: (PRIORITY.get(c["issued_by"], 99), _Newest(c["issued_at"], c.get("seq") or 0)))
        mode_set = False
        knob_set: set[str] = set()
        for c in commands:  # 並べた順（優先順位が強く、同じなら新しい物が前）に最初に当たった物を採る（S-5.5、D-4.2）
            if c["target"] not in (plugin_id, "all"):
                continue
            if c["op"] == "set_mode" and not mode_set:
                requested = c["args"].get("mode", mode)
                if (c["target"] == "all" and requested == "run"
                        and not self.store.run_target_in_scope(c["id"], plugin_id)):
                    continue
                if requested in (manifest.get("capabilities") or []):
                    mode = requested
                    mode_set = True
            elif c["op"] == "set_knob":
                name = c["args"].get("name")
                if name in knobs and name not in knob_set:
                    knobs[name] = c["args"].get("value")
                    knob_set.add(name)
        return {"plugin": plugin_id, "mode": mode, "knobs": knobs, "revision": p["revision"]}

    def _targets_of(self, target: str) -> list[dict]:
        plugins = [p for p in self.store.list_plugins() if p["id"] not in CORE_COMPONENTS]
        if target == "all":
            return [p for p in plugins if p["status"] == "registered"]
        p = self.store.get_plugin(target)
        return [p] if p is not None and p["status"] == "registered" and target not in CORE_COMPONENTS else []

    # ---- 命令の発行（S-5.3、S-5.4、D-4.2）----
    @serialized
    def issue(self, body: dict, *, issued_by: str) -> dict:
        if errs := schemas.errors("control-command-request", body):
            self._rejected("-", body, "; ".join(errs[:5]))
            raise Reject(422, "bad_request", "; ".join(errs[:5]))
        command = {
            "id": ids.new_ulid(), "target": body["target"], "op": body["op"], "args": body.get("args") or {},
            "issued_by": issued_by, "issued_at": timeutil.now_ts(),
            "expires_at": body.get("expires_at"), "on_expiry": body.get("on_expiry") or "ask_issuer",
            "state": "active", "renewals": 0, "reason": body.get("reason", ""), "corr": body.get("corr"),
        }
        self._validate(command, body)
        if command["op"] not in ("set_mode", "set_knob", "exclude_resource"):
            self._rejected(command["id"], body, "操作を対象へ渡す API が未定義")
            raise Reject(501, "operation_not_implemented", "S-5.3/S-4.4: 操作を対象へ渡す API が未定義")
        schemas.validate("control-command", clean_command(command))
        if command["op"] == "exclude_resource":
            self._require_exclusion_sync(command)
        with self._limits_transaction() if self._changes_limits(command) else contextlib.nullcontext():
            if self._changes_limits(command):
                self._freeze_limits()
            run_targets = ([p["id"] for p in self._targets_of("all")]
                           if command["target"] == "all" and command["op"] == "set_mode"
                           and command["args"].get("mode") == "run" else None)
            self.store.put_command(command, run_targets=run_targets)
            if self._changes_limits(command):
                self.sync_limits()
        self._command_event("control.issued", command)
        for p in self._targets_of(command["target"]):
            self.store.bump_revision(p["id"])
        if command["op"] == "set_knob":
            for p in self._targets_of(command["target"]):
                self._recompute_user_set(p["id"])
        if command["op"] != "exclude_resource":
            self.apply_to(command)
        return command

    def _recompute_user_set(self, plugin_id: str) -> None:
        """利用者設定の印（S-4.3 の 7）: 生きている user の set_knob の対象の knob に印を付ける。"""
        p = self.store.get_plugin(plugin_id)
        if p is None:
            return
        marks = {name for c in self.store.living_commands()
                 if c["issued_by"] == "user" and c["op"] == "set_knob" and c["target"] in (plugin_id, "all")
                 for name in [c["args"].get("name")] if name}
        p["user_set_knobs"] = {name: True for name in marks}
        self.store.upsert_plugin(p)

    def _validate(self, command: dict, body: dict) -> None:
        """S-5.4 の 1〜10。外れたら Reject（400）して control.rejected を書く。"""
        target, op, args, issued_by = command["target"], command["op"], command["args"], command["issued_by"]
        reason = command["reason"]

        def fail(code: str, msg: str, rule: str) -> None:
            self._rejected(command["id"], body, f"{code}: {msg}")
            raise Reject(400, code, f"S-5.4 の {rule}: {msg}", rule=rule)

        # 1. target が登録済みのプラグインである（中核ではない）
        if body.get("issued_by") and body["issued_by"] != issued_by:
            fail("issuer_mismatch", f"本文の issued_by（{body['issued_by']}）が資格（{issued_by}）と違う", "S-5.3")
        targets = self._targets_of(target)
        if not targets:
            fail("unknown_target", f"target {target} は登録済みのプラグインでない（中核は対象にできない）", "1")
        # 5. 管理役と予備役の命令は1対象
        if issued_by not in ("user", "inspection", "emergency") and target == "all":
            fail("multi_target_forbidden", "管理役・予備役の1つの命令の対象は1プラグイン（全体命令は不可）", "5")
        # 6. reason が空でない
        if not (reason or "").strip():
            fail("reason_required", "reason が空", "6")
        # 期限（S-5.4 の 4、10、S-5.6）
        issued_at = timeutil.parse_ts(command["issued_at"])
        expires_at = command.get("expires_at")
        if op == "exclude_resource":
            until = args.get("until")
            if not until or timeutil.parse_ts(until) <= timeutil.now():
                fail("until_not_future", "exclude_resource の until は未来であること", "8")
            if not expires_at or expires_at != until:
                fail("until_mismatch", "exclude_resource は until と expires_at の一致が要る", "8")
            if command["on_expiry"] != "release":
                fail("on_expiry_required", "exclude_resource は on_expiry: release が要る", "8")
        if issued_by != "user":
            if not expires_at:
                fail("lease_required", f"issued_by={issued_by} の命令には期限が要る", "4")
            limit = (timedelta(hours=float(self.defaults.get("drain_max_h"))) if issued_by == "inspection"
                     else timedelta(minutes=float(self.defaults.get("control_max_lease_min"))))
            if timeutil.parse_ts(expires_at) <= timeutil.now():
                fail("lease_not_future", "期限が過去", "4")
            if timeutil.parse_ts(expires_at) - issued_at > limit:
                fail("lease_too_long",
                     f"期限が発行時刻から {'drain_max_h' if issued_by == 'inspection' else 'control_max_lease_min'} を超える",
                     "4")
        if issued_by == "inspection" and command["on_expiry"] != "release":
            fail("on_expiry_required", "点検係の命令は on_expiry: release が必須", "S-5.6")
        # op ごとの検査
        for p in targets:
            manifest = p["manifest"]
            if op == "set_mode":
                mode = args.get("mode")
                if mode not in MODES:
                    fail("bad_mode", f"mode {mode} が S-5.2 の表に無い", "2")
                if mode not in (manifest.get("capabilities") or []):
                    fail("capability_mismatch", f"{p['id']} の capabilities に {mode} が無い", "2")
                if mode == "run":
                    if target != "all" and not (p.get("_declared_outputs", manifest.get("outputs")) or []):
                        permission_ids = args.get("permission_ids")
                        operation = f"plugin.run:{p['id']}"
                        if (not isinstance(permission_ids, list) or not permission_ids
                                or any(not isinstance(pid, str) for pid in permission_ids)):
                            self._rejected(command["id"], body, "permission_ids_required")
                            raise Reject(403, "permission_ids_required", "run requires matching permission_ids")
                        for pid in permission_ids:
                            perm = self.store.get_permission(pid)
                            if (not perm or not perm["enabled"] or operation not in perm["operations"]
                                    or perm.get("conditions")):
                                self._rejected(command["id"], body, "permission_ids_required")
                                raise Reject(403, "permission_ids_required", "run permission does not match")
                    validator = self.validate_delivery_scope
                    if validator is not None:
                        validator(command, body, p)
                    if set(manifest.get("outputs") or []) & SENDING_OUTPUTS:
                        if issued_by != "user":
                            fail("user_only", "送信処理の開始は利用者だけ", "9")
                        fail("destination_contract_unavailable", "S-23.1 の送信対象一覧を照合する API が未定義", "9")
                    outputs = set(manifest.get("outputs") or [])
                    sending = bool(outputs & SENDING_OUTPUTS)
                    permission_ids = args.get("permission_ids")
                    if sending and not permission_ids:
                        fail("permission_ids_required",
                             f"{p['id']} は送信対象を持つ処理。run にするには permission_ids が要る", "9")
                    if permission_ids:
                        for pid in permission_ids:
                            perm = self.store.get_permission(pid) if isinstance(pid, str) else None
                            if perm is None or not perm["enabled"]:
                                fail("permission_invalid", f"許可 {pid} が無いまたは無効", "9")
                            elif outputs and not (set(perm["operations"]) & outputs):
                                fail("permission_scope", f"許可 {pid} の操作が {p['id']} の宣言（outputs）に含まれない", "9")
            elif op == "set_knob":
                name = args.get("name")
                spec = (manifest.get("knobs") or {}).get(name)
                if spec is None:
                    fail("knob_not_declared", f"{p['id']} は調整項目 {name} を宣言していない", "3")
                composite = (p["id"] == "worker-pool" and name in
                             {"role_candidates", "family_cap", "model_cap", "model_descriptions"}
                             and spec["type"] in ("list", "string"))
                if composite:
                    from .worker_pool_settings import validate_settings
                    from axcommon.client import service_url
                    try:
                        client = self.resources_client or self._client(service_url("resources"), None)
                        resources = client.get("/v1/resources", params={"kind": "model"})
                        err = validate_settings(name, args.get("value"), resources)
                    except (Unavailable, ApiError, ValueError, TypeError, KeyError, OSError):
                        self._rejected(command["id"], body, "resources unavailable")
                        raise Reject(503, "resources_unavailable", "モデル登録簿を照合できない") from None
                    if err:
                        fail("knob_out_of_range", f"{name}: {err}", "3")
                elif err := knob_check(spec, args.get("value")):
                    fail("knob_out_of_range", f"{name}: {err}", "3")
                who = spec.get("who")
                if who and issued_by not in who:
                    fail("who_not_allowed", f"{name} を変えられるのは {who}（S-4.3 の who）", "7")
                if p["user_set_knobs"].get(name) and issued_by != "user":
                    fail("user_set_knob", f"{name} は利用者設定の印がある値。利用者以外は変えられない", "7")
            elif op in ("cancel", "run_now", "include_resource"):
                pass  # op 固有の追加の検査は S-5.4 の表の外
        # 10. 司令塔の命令は期限が必須（期限なしにしない）
        if issued_by == "commander" and not expires_at:
            fail("lease_required", "司令塔の命令は期限が必須", "10")

    # ---- /apply への適用（S-4.4、D-4.2）----
    def _client(self, base_url: str, token: str | None) -> ApiClient:
        kw = {"transport": self._applier_transport} if self._applier_transport else {}
        return ApiClient(base_url, token=token, **kw)

    def _apply_target_url(self, p: dict) -> str:
        from axcommon.client import service_url

        if p["kind"] == "subloop":
            return service_url(f"sl-{p['id']}")
        if p["kind"] == "timer":
            return service_url("timer-runner")
        return service_url(p["id"])

    def apply_to(self, command: dict) -> list[dict]:
        """対象の POST /apply を呼ぶ。control の資格（control-apply-token）だけが通る（F-18.4）。
        失敗しても命令は生きる（対象が control_poll_s ごとに effective を読むため。D-4.2）。"""
        from axcommon.creds import read_secret_optional

        token = read_secret_optional("control-apply-token")
        results = []
        for p in self._targets_of(command["target"]):
            if not token:
                results.append({"plugin": p["id"], "applied": False, "error": "SecretMissing"})
                continue
            eff = self.effective(p["id"])
            client = self._client(self._apply_target_url(p), token)
            try:
                client.post("/apply", {"mode": eff["mode"], "knobs": eff["knobs"], "revision": eff["revision"]})
            except (Unavailable, ApiError) as e:
                log.info("%s への /apply が失敗（対象が effective を読む）: %s", p["id"], type(e).__name__)
                results.append({"plugin": p["id"], "applied": False, "error": type(e).__name__})
                continue
            try:
                status = client.get("/status")
                if (isinstance(status, dict) and status.get("mode") == eff["mode"]
                        and status.get("knobs") == eff["knobs"]):
                    self._command_event("control.applied", command, {"plugin": p["id"], "revision": eff["revision"]})
                    results.append({"plugin": p["id"], "applied": True, "verified": True})
                else:
                    results.append({"plugin": p["id"], "applied": True, "verified": False})
            except (Unavailable, ApiError):
                results.append({"plugin": p["id"], "applied": True, "verified": False})
        return results

    def _require_exclusion_sync(self, command: dict) -> None:
        if self.exclusion_adapter is None:
            raise Reject(503, "exclusion_unavailable", "temporary exclusion service unavailable")
        try:
            self.exclusion_adapter.sync(command, active=True)
        except Exception:
            log.warning("temporary exclusion synchronization failed")
            raise Reject(503, "exclusion_unavailable", "temporary exclusion service unavailable") from None

    def _sync_exclusion(self, command: dict) -> None:
        if self.exclusion_adapter is None:
            return
        try:
            self.exclusion_adapter.sync(command, active=command["state"] != "released")
        except Exception:
            log.warning("temporary exclusion adapter unavailable")

    # ---- 期限（S-5.6、D-4.3）----
    @serialized
    def sweep_expiry(self) -> list[dict]:
        changed = []
        for c in self.store.list_commands("WHERE state='active'", []):
            exp = c.get("expires_at")
            if not exp or timeutil.parse_ts(exp) > timeutil.now():
                continue
            if c["on_expiry"] == "release":
                try:
                    after = self._release(c, by="expiry", reason="期限で自動的に解除（S-5.6）")
                except Reject as exc:
                    if exc.code != "limits_unavailable":
                        raise
                    continue
            else:
                after = dict(c)
                after["state"] = "expired_pending"
                self.store.put_command(after)
                self._command_event("control.expired_pending", after)
            changed.append(after)
        if self.exclusion_adapter is not None:
            for command in self.store.list_commands("WHERE op=?", ["exclude_resource"]):
                self._sync_exclusion(command)
        return changed

    @serialized
    def release(self, command_id: str, *, by: str, reason: str = "") -> dict:
        c = self.store.get_command(command_id)
        if c is None:
            raise Reject(404, "not_found", f"命令 {command_id} が無い")
        if by != c["issued_by"] and by not in ("user", "emergency"):
            self._rejected(command_id, clean_command(c), "解除は出した者だけ")
            raise Reject(403, "forbidden", "解除は出した者だけ（S-5.6）")
        if c["state"] == "released":
            return c
        return self._release(c, by=by, reason=reason)

    def _release(self, c: dict, *, by: str, reason: str) -> dict:
        after = dict(c)
        after["state"] = "released"
        after["released_by"] = by
        after["released_reason"] = reason
        with self._limits_transaction() if self._changes_limits(c) else contextlib.nullcontext():
            if self._changes_limits(c):
                self._freeze_limits()
            self.store.put_command(after)
            if self._changes_limits(c):
                self.sync_limits()
        self._command_event("control.released", after, {"by": by})
        for p in self._targets_of(c["target"]):
            self.store.bump_revision(p["id"])
        if c["op"] == "set_knob":
            for p in self._targets_of(c["target"]):
                self._recompute_user_set(p["id"])
        if after["op"] == "exclude_resource":
            self._sync_exclusion(after)
        else:
            self.apply_to(after)
        return after

    @serialized
    def renew(self, command_id: str, body: dict, *, by: str) -> dict:
        expires_at = body.get("expires_at")
        if not expires_at:
            raise Reject(422, "bad_request", "延長には新しい期限が要る（S-5.6）")
        if not (body.get("reason") or "").strip():
            raise Reject(422, "reason_required", "延長には理由が要る（S-5.6）")
        c = self.store.get_command(command_id)
        if c is None:
            raise Reject(404, "not_found", f"命令 {command_id} が無い")
        if by != c["issued_by"] and by not in ("user", "emergency"):
            raise Reject(403, "forbidden", "延長は出した者だけ（S-5.6）")
        limit = int(self.defaults.get("control_max_renewals"))
        if c["renewals"] >= limit:
            raise Reject(409, "renewals_exhausted", f"延長は1つの命令につき {limit} 回まで（S-17 control_max_renewals）")
        if c["state"] == "released":
            raise Reject(409, "command_released", "解除した命令は延長できない")
        expiry = parse_time(expires_at, "expires_at")
        if expiry <= timeutil.now():
            raise Reject(422, "lease_not_future", "新しい期限が過去")
        max_lease = (timedelta(hours=float(self.defaults.get("drain_max_h"))) if c["issued_by"] == "inspection"
                     else timedelta(minutes=float(self.defaults.get("control_max_lease_min"))))
        if c["issued_by"] != "user" and expiry - timeutil.now() > max_lease:
            raise Reject(400, "lease_too_long", "新しい期限が保持期間の上限を超える")
        after = dict(c)
        after["expires_at"] = timeutil.format_ts(expiry)
        if after["op"] == "exclude_resource":
            after["args"] = {**after["args"], "until": after["expires_at"]}
        after["renewals"] = c["renewals"] + 1
        after["state"] = "active"
        if after["op"] == "exclude_resource":
            self._require_exclusion_sync(after)
        with self._limits_transaction() if self._changes_limits(c) else contextlib.nullcontext():
            if self._changes_limits(c):
                self._freeze_limits()
            self.store.put_command(after)
            if self._changes_limits(c):
                self.sync_limits()
        self._command_event("control.renewed", after, {"reason": body["reason"]})
        for p in self._targets_of(c["target"]):
            self.store.bump_revision(p["id"])
        return after

    def list_commands(self, *, state: str | None = None, issued_by: str | None = None) -> list[dict]:
        where, params = [], []
        if state:
            where.append("state=?")
            params.append(state)
        if issued_by:
            where.append("issued_by=?")
            params.append(issued_by)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        return self.store.list_commands(w, params)

    # ---- 項目の中継（S-5.7）----
    def relay_items(self, plugin_id: str, body: dict) -> tuple[int, Any]:
        """本文の項目を ax-work の網で sl-<id> の POST /items へそのまま渡す。中継は項目を保存しない。
        応答は（状態、本体）としてそのまま返す（受付 202 か inbox_full 409。S-5.7）。"""
        import json as _json

        from axcommon.creds import read_secret_optional

        p = self.store.get_plugin(plugin_id)
        if p is None or p["status"] != "registered" or plugin_id in CORE_COMPONENTS:
            raise Reject(404, "not_found", f"プラグイン {plugin_id} が登録に無い")
        if not p["manifest"].get("accepts_items"):
            raise Reject(409, "not_accepting_items", f"{plugin_id} は accepts_items: true でない")
        token = read_secret_optional("control-apply-token")
        if not token:
            raise Reject(503, "unavailable", "control-apply-token が無いため中継できない")
        client = self._client(self._apply_target_url(p), token)
        headers = {"Accept": "application/json"}
        raw = _json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        if client._token:
            headers["Authorization"] = "Bearer " + client._token
        try:
            status, _h, resp_raw = client._transport("POST", client.base_url + "/items", headers, raw, client.timeout)
        except Unavailable:
            raise Reject(503, "unavailable", f"サブループ {plugin_id} が落ちている。入口が再送する") from None
        parsed: Any = None
        if resp_raw:
            try:
                parsed = _json.loads(resp_raw.decode("utf-8"))
            except ValueError:
                parsed = {"raw": resp_raw.decode("utf-8", "replace")}
        if status >= 500:
            raise Reject(503, "unavailable", f"サブループ {plugin_id} が {status} を返した。入口が再送する")
        return status, parsed

    # ---- 継続許可（S-5.7、S-20.3）----
    def list_permissions(self) -> list[dict]:
        return self.store.list_permissions()

    def check_permission(self, body: dict) -> dict:
        if errs := schemas.errors("permission-check", body, fragment="/$defs/request"):
            raise Reject(422, "bad_request", "; ".join(errs[:5]))
        for perm in self.store.list_permissions():
            if not perm["enabled"]:
                continue
            if perm["project"] != body["project"]:
                continue
            if perm["repo"] != body["repo"]:
                continue
            if perm["destination"] != body["destination"]:
                continue
            if body["operation"] not in perm["operations"]:
                continue
            if perm["conditions"]:
                out = {"allowed": False, "permission_id": perm["id"], "reason": "条件を照合する API が未定義（S-20.3）"}
                self.emit("permission.checked", body.get("repo") or "-", {
                    "permission_id": perm["id"], "operation": body["operation"], "result": "denied"})
                return out
            out = {"allowed": True, "permission_id": perm["id"],
                   "reason": f"許可 {perm['id']} の範囲（条件は操作の直前に別に確かめる。S-20.3）"}
            self.emit("permission.checked", body.get("repo") or "-", {
                "permission_id": perm["id"], "operation": body["operation"], "result": "allowed"})
            return out
        out = {"allowed": False, "permission_id": None,
               "reason": "当てはまる有効な許可が無い（S-20.3）"}
        self.emit("permission.checked", body.get("repo") or "-", {
            "permission_id": None, "operation": body.get("operation"), "result": "denied"})
        return out

    def create_permission(self, body: dict, *, by: str) -> dict:
        if by != "user":
            raise Reject(403, "forbidden", "継続許可を作成できるのは利用者だけ")
        p = dict(body)
        p.setdefault("id", ids.new_ulid())
        if not ids.is_ulid(p["id"]):
            raise Reject(422, "bad_request", "許可の id は ULID が必須")
        if self.store.get_permission(p["id"]):
            raise Reject(409, "permission_exists", "同じ id の許可がある")
        ops = p.get("operations") or []
        if not isinstance(ops, list) or not all(isinstance(o, str) for o in ops) or len(set(ops)) != len(ops):
            raise Reject(422, "bad_request", "operations は重複のない文字列の一覧が必須")
        if not ops:
            raise Reject(422, "bad_request", "操作の集合が空（S-20.3）")
        def known_operation(operation):
            if operation in OPERATION_KINDS and operation not in {"comment", "label"}:
                return True
            if not operation.startswith("plugin.run:"):
                return False
            plugin = self.store.get_plugin(operation.removeprefix("plugin.run:"))
            return bool(plugin and plugin["status"] == "registered" and not (plugin["manifest"].get("outputs") or []))

        unknown = [o for o in ops if not known_operation(o)]
        if unknown:
            raise Reject(422, "bad_request", f"未登録の操作種類: {unknown}（S-20.3 の操作種類の登録簿）")
        for key in ("project", "repo", "destination", "source_instruction"):
            if not isinstance(p.get(key), str) or not p[key].strip() or p[key] == "*":
                raise Reject(422, "bad_request", f"{key} が空（対象が特定できない値は有効にしない。S-20.3）")
        if not isinstance(p.get("conditions", {}), dict):
            raise Reject(422, "bad_request", "conditions は object が必須")
        now = timeutil.now_ts()
        p.setdefault("created_at", now)
        p.setdefault("updated_at", now)
        p["enabled"] = True
        p["created_by"] = by
        self.store.put_permission(p)
        self.emit("permission.changed", p["id"], {
            "permission_id": p["id"], "operation": "create", "result": "ok",
            "source_instruction": p["source_instruction"]})
        return p

    def disable_permission(self, permission_id: str, *, by: str) -> dict:
        if by != "user":
            raise Reject(403, "forbidden", "継続許可の無効化は利用者だけ")
        perm = self.store.get_permission(permission_id)
        if perm is None:
            raise Reject(404, "not_found", f"許可 {permission_id} が無い")
        after = dict(perm)
        after["enabled"] = False
        after["updated_at"] = timeutil.now_ts()
        self.store.put_permission(after)
        self.emit("permission.changed", permission_id, {
            "permission_id": permission_id, "operation": "disable", "result": "ok"})
        return after

    # ---- タイマー（S-7.8、D-4.4）----
    @serialized
    def create_timer(self, body: dict, *, issued_by: str) -> dict:
        if issued_by != "manager":
            raise Reject(403, "manager_only", "期限付きタイマーを作れるのは管理役だけ（S-7.8）")
        if not isinstance(body, dict):
            raise Reject(422, "bad_request", "タイマーは object が必須")
        allowed = {"id", "at", "every_s", "action", "reason", "expires_at", "issued_by", "target", "command_id"}
        if set(body) - allowed or (body.get("issued_by", issued_by) != issued_by):
            raise Reject(422, "bad_request", "タイマーの未知の項目または issued_by の不一致")
        if (body.get("at") is not None) == (body.get("every_s") is not None):
            raise Reject(422, "bad_request", "at と every_s の片方だけが必須（S-7.8）")
        action = body.get("action")
        if action not in TIMER_ACTIONS:
            raise Reject(422, "bad_request", f"action は {TIMER_ACTIONS} のどれか（S-7.8）")
        if not isinstance(body.get("reason"), str) or not body["reason"].strip():
            raise Reject(422, "reason_required", "reason が空")
        if action == "plugin.run_now" and (not isinstance(body.get("target"), str)
                                           or body["target"] == "all" or not self._targets_of(body["target"])):
            raise Reject(422, "unknown_target", "plugin.run_now は登録済みのプラグイン1つが必須")
        if action == "control.release":
            if not isinstance(body.get("command_id"), str):
                raise Reject(422, "bad_request", "command_id が必須")
            command = self.store.get_command(body["command_id"])
            if command is None or command["issued_by"] != issued_by:
                raise Reject(403, "timer_command_owner", "control.release は自分の命令だけ（S-7.8）")
        now = timeutil.now()
        interval = body.get("every_s")
        if interval is not None and (type(interval) is not int or interval < 1):
            raise Reject(422, "bad_request", "every_s は正の整数")
        try:
            at = parse_time(body["at"], "at") if body.get("at") is not None else now + timedelta(seconds=interval)
        except OverflowError:
            raise Reject(422, "bad_request", "every_s が大きすぎる") from None
        if at <= now:
            raise Reject(422, "at_not_future", "at は未来であること")
        max_days = float(self.defaults.get("timer_max_days"))
        expires = parse_time(body["expires_at"], "expires_at") if body.get("expires_at") else now + timedelta(days=max_days)
        if expires <= now:
            raise Reject(422, "expires_not_future", "expires_at は未来であること")
        if expires - now > timedelta(days=max_days):
            raise Reject(422, "timer_max_days", f"タイマーの寿命は {max_days} 日まで（S-17 timer_max_days）")
        if at >= expires:
            raise Reject(422, "timer_never_due", "起動予定は expires_at より前が必須")
        timer_id = body.get("id") or ids.new_ulid()
        if not ids.is_ulid(timer_id):
            raise Reject(422, "bad_request", "id は ULID が必須")
        if self.store.get_timer(timer_id):
            raise Reject(409, "timer_exists", "同じ id のタイマーがある")
        self.sweep_timers()
        max_count = int(self.defaults.get("timer_max_count"))
        if len(self.store.list_timers("active")) >= max_count:
            raise Reject(409, "timer_max_count", f"同時に生きているタイマーは {max_count} まで")
        t = {"id": timer_id, "at": timeutil.format_ts(at), "every_s": interval,
             "action": action, "reason": body["reason"], "target": body.get("target"),
             "command_id": body.get("command_id"), "issued_by": issued_by, "created_at": timeutil.now_ts(),
             "expires_at": timeutil.format_ts(expires), "state": "active", "fired_count": 0}
        self.store.put_timer(t)
        return t

    def list_timers(self, state: str | None = None) -> list[dict]:
        if state is not None and state not in TIMER_STATES:
            raise Reject(422, "bad_request", "タイマーの state が不正")
        return self.store.list_timers(state)

    @serialized
    def sweep_timers(self) -> list[dict]:
        now = timeutil.now()
        changed = []
        for t in self.store.list_timers("active"):
            if parse_time(t["expires_at"], "expires_at") <= now:
                t["state"] = "expired"
                self.store.put_timer(t)
                changed.append(t)
            elif t["action"] == "control.release" and parse_time(t["at"], "at") <= now:
                self.release(t["command_id"], by=t["issued_by"], reason=t["reason"])
                changed.append(self.fire_timer(t["id"]))
        return changed

    @serialized
    def fire_timer(self, timer_id: str) -> dict:
        """timer-runner の実行報告。単発の重複報告は値を変えない。予定前は拒否する。"""
        t = self.store.get_timer(timer_id)
        if t is None:
            raise Reject(404, "not_found", f"タイマー {timer_id} が無い")
        if t["state"] != "active":
            return t
        now = timeutil.now()
        if parse_time(t["expires_at"], "expires_at") <= now:
            t["state"] = "expired"
        elif parse_time(t["at"], "at") > now:
            raise Reject(409, "timer_not_due", "予定前のタイマーは発火できない")
        else:
            t["fired_count"] += 1
            if t["every_s"]:
                t["at"] = timeutil.format_ts(now + timedelta(seconds=t["every_s"]))
            else:
                t["state"] = "fired"
        self.store.put_timer(t)
        return t

    # ---- 検査の口（S-5.7、F-14.3）----
    def validate(self, kind: str, body: Any) -> dict:
        if kind == "manifest":
            from .registry import manifest_errors

            errs = manifest_errors(body)
        elif kind == "graph":
            from .graphcheck import errors_for_graph

            errs = errors_for_graph(body)
        else:
            raise Reject(422, "bad_request", "kind は manifest か graph")
        out = {"ok": not errs, "errors": errs}
        if errs and kind == "manifest":
            out["errors"] = errs[:20]
        schemas.validate("validate", out, fragment="/$defs/response")
        return out
