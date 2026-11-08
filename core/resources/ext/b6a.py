"""b6a の差し込み口（core/resources/ext/b6a.py）。

このファイルは b6a が作る。バンドル b14b1、b18a、b18b が core/resources/ext/
に足す ext/*.py のための差し込み口で、register(app) を持つ。
axcommon.ext.load_extensions(app, ext_dir) が core/resources/ext の指すディレクトリから読む。

この b6a は区切りとして空の register を持つ（環境や箱の実装は別の束）。
他束の ext/b14b1.py, ext/b18a.py, ext/b18b.py はここでは作らない。
"""
from __future__ import annotations


def register(app):
    # 区切り。実実装は b14b1/b18a/b18b が足す。ここでは失敗しない空の登録だけ。
    environ_state = getattr(app.state, "ax_resources_envs", None)
    return None
