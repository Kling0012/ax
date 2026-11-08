"""8081（作業側の口）で書いてよい kind（F-18.4、S-3.3）。

F-18.4: 「kind は S-3.3 で書き手が『サブループ』『各部品』の物か、そのプラグインの manifest が宣言した物」。
S-3.3 の書き手の列で、サブループ（収集・変更・公開・CI を含む）、各部品、当該部品、当該プラグイン、通知ノード、選別担当にあたる kind を並べた。
manifest が宣言する kind の項目は manifest スキーマに無いため、manifest_kinds の提供関数を差し替えられる形にした（既定は空。OPEN.md）。
"""
from __future__ import annotations

from typing import Callable

WORK_SIDE_KINDS = frozenset({
    "run.state", "item.state", "node.state",
    "resource.repeat_failure", "error.classified", "health.beat",
    "change.cycle", "change.route", "change.lesson", "change.notification",
    "release.pending", "release.batch", "release.result",
    "publish.check", "ci.collection",
    "lesson.candidate", "lesson.selected",
    "question.notified", "notification.failed", "digest.generated", "work.cancelled", "work.resumed", "safety.rejected",
    "automation.observed", "automation.delivery", "automation.collect_checked",
})

# 作業側の口では、入口が確かめた値だけを入れる項目（S-3.2）を本文から受けない
RESERVED_SOURCE_FIELDS = ("principal", "session", "via")

ManifestKinds = Callable[[str], "frozenset[str] | set[str]"]


def no_manifest_kinds(plugin_id: str) -> frozenset[str]:
    return frozenset()


def kind_allowed(plugin_id: str, kind: str, manifest_kinds: ManifestKinds = no_manifest_kinds) -> bool:
    return kind in WORK_SIDE_KINDS or kind in manifest_kinds(plugin_id)
