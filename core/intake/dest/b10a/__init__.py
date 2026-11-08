"""入口の行き先 `emergency` の登録（差し込み口。core/intake/dest/<pkg>/__init__.py の register(app)）。

入口は __init__.py をファイルから読み込む（パッケージ名が違う）ので、相対 import は使わず、絶対 import で読む。
`python -m core.intake.dest.b10a` は `emergency` サービス（service.py）。
"""
from __future__ import annotations


def register(app) -> None:
    from core.intake.dest.b10a.handler import emergency_handler

    app.state.intake.dest_handlers["emergency"] = emergency_handler
