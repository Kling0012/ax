"""worker-pool の保存（SQLite）。jobs 表を持つ（D-7.5）。

worker-pool の jobs に session_id、resume_from、resume_required、連鎖の root、chain_length、
作業の場所 workdir_id、item_id、model、最後の試行の結果、finished_at、last_context を持つ。
再開の条件 (a)〜(d) は、この表の resume_from の行で worker-pool が確かめる（S-8.12）。
"""
from __future__ import annotations

from pathlib import Path

from .jobs import JobsStore

__all__ = ["JobsStore"]
