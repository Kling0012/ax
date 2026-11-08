"""点検係（B11。S-13、D-12）。定期点検の係。中核の部品で、制御命令では変えない（S-13.1）。

予定（間隔の初期値と範囲は config/inspection.yaml。今の値は自分のボリュームに持つ）→ 段 1 予告 → 段 2 減らし込み → 段 3 待つ →
段 4〜7 管理役に頼む（inspection.phase=review_request）→ 段 8 再開。
黙ったら自動で戻るのはここだけ（S-13.3、R-O6）。点検の外では、命令が黙って戻ることはない。
標準ライブラリ中心。starlette を使うのは app.py だけ。
"""
from __future__ import annotations

VERSION = "0.1.0"
COMPONENT = "inspector"

# inspection.phase の phase の名前（SPEC に列挙が無く、オブザーバー（core/observer/checks.py の DRAIN_PHASES）に合わせた。OPEN.md）
PHASE_SCHEDULE = "schedule"
PHASE_NOTICE = "notice"
PHASE_DRAINING = "draining"
PHASE_WAIT = "wait"
PHASE_REVIEW_REQUEST = "review_request"
PHASE_REVIEW = "review"
PHASE_RESUME = "resume"
