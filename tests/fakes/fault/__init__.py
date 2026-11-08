"""B13 故障注入の偽の部品（tests/fakes/fault/）。T-8、T-12、T-14、T-17 が使う。API の一覧は docs/b9/fault-api.md。

    FaultNet   部品間の HTTP を止める・遅らせる・偽の応答を返す・応答だけ失う。F-18.4 の到達表（apply_f184）
    Chaos      プロセスの中のメソッドに、例外・遅延・でたらめな値・固まりを注入する
    run_rounds プラグイン 1 つ = 独立した周回として走らせ、1 つの失敗が他へ伝わらないことを数える
偽のモデルの入口は tests/fakes/model_gateway.py（b0）。管理役向けの worker-pool の偽は tests/fakes/b9/fake_pool.py。
"""
from .chaos import Chaos  # noqa: F401
from .net import BOTH, CORE_ONLY, Call, Fault, FaultNet, zones_of  # noqa: F401
from .rounds import RoundStats, run_rounds  # noqa: F401
