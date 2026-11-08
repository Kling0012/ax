"""予備役（B10。S-11.3、S-11.4、D-10.2、R-O4）。

管理役が止まったとき（オブザーバーの警報の宛先 `backup`）にオブザーバーが呼ぶ。LLM はここでは使わない。
管理役と同じ周回の関数（core/manager のモジュール）を、役割 `backup_manager`、`issued_by=backup` で 1 回呼ぶだけで、
判断の中身（モデルの選択を含む）は管理役の側にある（モデルは S-8.3 の表のとおり。利用者への確認は OPEN.md）。

予備役の呼び出しごとに必ず行うこと（S-11.3 の 3、D-10.2）:
  - 根本原因の ToDo（`title` が「管理役の不調: …」）を 1 件作る。台帳に届かないときは自分の保存先に溜め、戻ったら作る。
  - 直近 `backup_window_days` 日に `backup_limit` 回以上動いたら、その起動の根本原因の ToDo に最優先の印を付け、
    利用者向けのカードにも出す（S-11.4、R-O4）。

差し込み: core/observer/ext/b10a.py の register(app) が `Observer.set_hook("backup", …)` で渡す。
"""
VERSION = "0.1.0"
COMPONENT = "observer"

ROLE = "backup_manager"        # 予備役の役割（S-8.3）
ISSUED_BY = "backup"           # 制御命令の出した者（S-5.3）
TITLE_PREFIX = "管理役の不調: "  # 根本原因の ToDo の題の頭（S-11.3 の 3）

# 根本原因の ToDo の `reason`（印）。点検係（S-13.2 の段 6、「予備役の件が最優先」）はこの接頭辞で探す。
REASON_ROOT = "backup:root_cause"
REASON_TOP = "backup:root_cause:top"        # 頻発したとき（S-11.4）。次の点検の最優先
REASON_CARD = "backup:frequent_card"        # 利用者向けのカード（R-O4）
