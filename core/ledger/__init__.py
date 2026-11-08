"""台帳（B3。S-9、D-8）。

ToDo の台帳・遷移・依存・カード（S-9.1〜S-9.3）、目標・約束・予定・集中状態・日次状態（S-9.4、S-9.5）、
判断の記録（S-9.6、D-8）を1つの SQLite に持ち、中核の口（8080）と作業側の口（8081。F-18.4 の表）の
2つの Starlette アプリを出す。

- 遷移の表（S-9.2）にない遷移は 409。理由のない waiting_user は 422。
- `user_only` の既定は理由から台帳が決める（USER_ONLY_REASONS。D-8）。作る側の申告では決まらない。
- 集中状態・日次状態の作業側の口（8081）の書き込みは `hibi-state` だけ（S-9.5、T-183）。
- 時系列記録の `item.state` のうち `data.todo_id` を持つ物を購読し、対応する ToDoを遷移させる（S-9.3、D-8）。
"""
from __future__ import annotations

# 遷移の表（S-9.2）
TRANSITIONS: dict[str, set[str]] = {
    "todo": {"running", "waiting_user", "parked", "dropped"},
    "running": {"review", "waiting_user", "parked", "todo", "done"},
    "review": {"done", "todo", "parked"},
    "waiting_user": {"todo", "done", "dropped"},
    "parked": {"todo", "dropped"},
    "done": set(),
    "dropped": set(),
}

# waiting_user にできる理由（S-9.2。必須）
WAITING_REASONS = {"physical", "secret_input", "money", "publish", "irreversible", "preference", "range_review"}

# user_only=true が固定の理由（S-9.2、D-8。USER_ONLY_REASONS はこの1つだけ）
USER_ONLY_REASONS = {"irreversible", "publish", "money", "secret_input"}

# 作業側の口（8081）で集中状態・日次状態を書けるのは hibi-state だけ（S-9.5、F-18.4）
HIBI_STATE_PLUGIN = "hibi-state"

# 集中状態・日次状態の中核の口（8080）の書き込み・読み取りの資格（D-8: hq・commander の資格）
STATE_CORE_CALLERS = {"hq", "commander"}

VERSION = "1.0.0"


class Reject(Exception):
    """API の拒否。status は HTTP の状態コード、code は schemas/api-error の code。"""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


def user_only_for(reason: str | None) -> bool:
    """理由から user_only の既定を決める（S-9.2）。irreversible/publish/money/secret_input は true 固定。"""
    return reason in USER_ONLY_REASONS


from .service import LedgerService  # noqa: E402
from .app import create_core_app, create_work_app  # noqa: E402

__all__ = [
    "TRANSITIONS", "WAITING_REASONS", "USER_ONLY_REASONS", "HIBI_STATE_PLUGIN", "STATE_CORE_CALLERS",
    "VERSION", "Reject", "user_only_for", "LedgerService", "create_core_app", "create_work_app",
]
