"""入口（intake。S-6.1、D-5）。

司令塔と HQ からの指示を受け、検証して、登録簿に載った行き先へ渡す。指示の中身は判断しない。
B5 の担当は骨格: 登録簿の読み込み（S-6.3）、検証（S-6.5、`instruction.rejected`）、行き先ハンドラの
自動読み込み（`dest/<pkg>/` と `ext/*.py` の register(app)）、commander ソケット（F-9.2）。
最初の行き先は `control.command` と `ledger.answer`。

後続の束の差し込み口:
- `core/intake/ext/<pkg>.py` / `core/intake/dest/<pkg>/__init__.py` の register(app)
- `app.state.intake.conflict_guard`（S-6.17 の衝突の検出。b12b3）
- `app.state.intake.approval_check`（S-6.10 の承認。b12b2）
- `app.state.intake.extra_listener_factories`（seat.sock、hq ソケット、gate 用の TCP。b12b1）
"""

__version__ = "0.1.0"
