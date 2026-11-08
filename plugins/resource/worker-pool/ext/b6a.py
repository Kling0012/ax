"""b6a の差し込み口（plugins/resource/worker-pool/ext/b6a.py）。

worker-pool の拡張の差し込み口。ここが b6a の register を持つ。
他束（b14b1/b18a/b18b）が足すファイルは core/resources/ext の側にある。
"""
from __future__ import annotations


def register(app):
    # 区切り。状態はここで持つ
    return None
