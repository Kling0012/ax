"""試験用ノード型（tests/fakes/b7/nodes/t/）の共有状態。試験ごとに reset() する。"""
LOG: list[tuple[str, str]] = []        # (item_id, node_id) の実行順
EXTERNAL: list[str] = []               # 外への操作の記録（鍵）。二重実行の検査に使う
COUNTS: dict[tuple, int] = {}          # (item_id, node_id) の呼び出し回数
CRASH_AFTER_OP = {"on": False}         # t.effect が外への操作の後で落ちる
CRASH_BEFORE_OP = {"on": False}        # launching の後、外への操作の前で落ちる


def reset():
    LOG.clear()
    EXTERNAL.clear()
    COUNTS.clear()
    CRASH_AFTER_OP["on"] = False
    CRASH_BEFORE_OP["on"] = False
