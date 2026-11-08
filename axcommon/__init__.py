"""AX の共通部品庫（D-2.2）。全イメージに入れる。

このパッケージの import は軽くする（標準ライブラリだけ）。重い依存（starlette、httpx、jsonschema、yaml）は
使うモジュールの中で import する。ホストのスクリプト（host/*.sh）が `python3 -m axcommon.profile` を呼ぶため。
除外: redact.py、peercred.py、ops.py、chat_json.py は別の束が作る（B0 は作らない）。
"""

__version__ = "0.1.0"


def publish_legacy_plugin_ids():
    """Return configured legacy publisher IDs without storing any in code."""
    import os
    return frozenset(value.strip() for value in os.environ.get(
        "AX_PUBLISH_LEGACY_PLUGIN_IDS", "").split(",") if value.strip())
