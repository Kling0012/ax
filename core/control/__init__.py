"""制御 API（B4。S-4、S-5、S-7.8、D-4）。

プラグインの登録簿（S-4.1、S-4.2）、制御命令の検証・適用・期限管理（S-5.3〜S-5.6、D-4.2、D-4.3）、
継続許可（S-5.7、S-20.3）、タイマーの登録と期限（S-7.8、D-4.4）、検査の口 POST /v1/validate（S-5.7）。
中核の口（8080）と作業側の口（8081。GET /v1/plugins* だけ。F-18.4 の表）の2つのアプリを出す。

- 登録は起動時に plugins/registry.yaml を読み、載っているプラグインだけを登録する（S-4.1）。
- `issued_by` は呼び出し元の資格から決め、命令の本文の値は信用しない（S-5.3）。
- 期限を過ぎた命令は自動では無効にしない。expired_pending にして効力を残す（S-5.6）。
"""
from __future__ import annotations

from axcommon import publish_legacy_plugin_ids

VERSION = "1.0.0"

# 優先順位（S-5.5）。数が小さいほど強い。
PRIORITY = {"emergency": 0, "user": 1, "inspection": 2, "commander": 3, "manager": 4, "backup": 4}

# 発行者別の専用トークン（S-5.3、compose の secrets。b0 の DECISIONS）
ISSUER_TOKENS = {"manager": "control-token-manager", "backup": "control-token-backup",
                 "inspection": "control-token-inspection", "emergency": "control-token-emergency"}

# core-tokens の表で入口を名指す名（D-5: issued_by は入口が決める）。入口は user / commander を主張できる。
INTAKE_CALLER = "intake"
INTAKE_ISSUERS = {"user", "commander"}

# 動作モード（S-5.2）
MODES = ("run", "collect_only", "poll_paused", "draining")

# 命令の操作（S-5.3）
OPS = ("set_mode", "set_knob", "exclude_resource", "include_resource", "cancel", "run_now")

# 操作種類の登録簿の初期値（S-20.3）。comment/label は継続許可に載せない日常の操作。
OPERATION_KINDS = {"merge", "release", "deploy", "publish", "post", "comment", "label",
                   "reply", "respond", "friend_request", "friend_accept"}

# タイマーの action（S-7.8）
TIMER_ACTIONS = ("ledger.recheck", "control.release", "plugin.run_now")

# 送信対象を持つ処理の出力の名（S-5.4 の 9。manifest の outputs と照らす）
SENDING_OUTPUTS = {"publish", "post", "reply", "respond", "friend_request", "friend_accept",
                   "x.post", "discord.send", "scheduled-publisher.publish", "scheduled-publisher.reply"}
SENDING_OUTPUTS.update(f"{plugin_id}.{operation}"
                       for plugin_id in publish_legacy_plugin_ids()
                       for operation in ("publish", "reply"))


class Reject(Exception):
    """API の拒否（S-5.4）。検証に外れた命令は control.rejected を書いて拒否する。"""

    def __init__(self, status: int, code: str, message: str = "", *, rule: str | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.rule = status, code, message, rule


from .service import ControlService  # noqa: E402
from .app import create_core_app, create_work_app  # noqa: E402

__all__ = ["VERSION", "PRIORITY", "ISSUER_TOKENS", "INTAKE_CALLER", "INTAKE_ISSUERS", "MODES", "OPS",
           "OPERATION_KINDS", "TIMER_ACTIONS", "SENDING_OUTPUTS", "Reject", "ControlService",
           "create_core_app", "create_work_app"]
