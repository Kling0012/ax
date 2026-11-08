"""管理役（B9。S-10、D-9）。周回して判断する中核の部品。

- 契機: 時系列記録の購読（error.classified の unknown、health.alert の宛先 manager、自分の control.expired_pending、
  resource.repeat_failure、inspection.phase、job.state の blocked）と、manager_tick_min ごとの時計（S-10.1）。
- 1 回の周回の順番（S-10.2）: 1 自分の命令（最初）、2 unknown、3 緩和、4 恒久修繕の ToDo、5 check_at を過ぎた parked、6 manager.cycle。
- モデルは worker-pool の POST /v1/jobs（役割 manager、runner chat-json。F-7.2）。モデルの入口にも鍵にも触れない。GPT と Claude は選ばない（S-10.3）。
- してよいこと・いけないこと（S-10.4）は policy.py の 1 つの関数で機械的に照らす。

標準ライブラリ中心。starlette を使うのは app.py だけ（ロジックの試験は starlette なしで動く）。
"""
from __future__ import annotations

VERSION = "0.1.0"
COMPONENT = "manager"

# 契機にする事象の種類（S-10.1）
TRIGGER_KINDS = ("error.classified", "health.alert", "control.expired_pending", "resource.repeat_failure", "inspection.phase", "job.state")

# 管理役が制御命令を出してはいけない相手（中核の部品。S-5.1「中核を制御する命令は受けない」、S-10.4）。
# 制御 API も登録簿に無い target を拒否する（S-5.4 の 1）。ここは二重の防御。
CORE_NAMES = frozenset({"intake", "control", "timeline", "ledger", "resources", "inspector", "manager", "observer", "hq", "gate",
                        "memory-connector", "archiver", "egress-proxy", "timer-runner", "backup", "ax-hostwatch"})


class ManagerError(Exception):
    pass
