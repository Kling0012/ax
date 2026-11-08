"""送信前の検査（S-22.4、R-H24、R-H25、T-55）。axcommon.redact の Redactor を使う。値は結果に持たない（種別と件数だけ）。

verdict.action:
  send    そのまま送る（当たりなし）
  mask    伏せ字にした本文を送る（配送先の on_hit が mask）
  block   送らない。reason は hit（秘密・個人情報が見つかった）か unverifiable（検査できない）
検査できない = Redactor が無い、または索引か利用者登録の一覧が読めていない（index_ok、pii_list_ok が偽）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .policy import DestinationPolicy


@dataclass
class Verdict:
    action: str
    title: str
    message: str
    reason: str | None = None
    categories: dict[str, int] = field(default_factory=dict)


class Guard:
    def __init__(self, redactor=None):
        self.redactor = redactor

    @property
    def verifiable(self) -> bool:
        r = self.redactor
        return r is not None and bool(getattr(r, "index_ok", False)) and bool(getattr(r, "pii_list_ok", False))

    def inspect(self, title: str, message: str, policy: DestinationPolicy) -> Verdict:
        # S-22.4 requires refusal for Discord even if a supplied policy is permissive.
        if policy.name == "discord":
            policy = replace(policy, on_hit="block", on_unverifiable="block")
        try:
            r = self.redactor
            if r is not None and getattr(r, "index_path", None) is not None:
                if not r.reload_index_if_changed():
                    return Verdict("block", "", "", reason="unverifiable")
            if r is not None and getattr(r, "pii_list_path", None) is not None:
                if not r.reload_pii_list():
                    return Verdict("block", "", "", reason="unverifiable")
        except Exception:
            return Verdict("block", "", "", reason="unverifiable")
        if not self.verifiable:
            if policy.on_unverifiable == "send":
                return Verdict("send", title, message, reason="unverifiable_sent")
            return Verdict("block", "", "", reason="unverifiable")
        try:
            rt = self.redactor.redact_text(title)
            rm = self.redactor.redact_text(message)
        except Exception:  # noqa: BLE001  検査器が例外を出したら検査不能として扱う
            if policy.on_unverifiable == "send":
                return Verdict("send", title, message, reason="unverifiable_sent")
            return Verdict("block", "", "", reason="unverifiable")
        hits = list(rt.hits) + list(rm.hits)
        if not hits:
            return Verdict("send", title, message)
        cats: dict[str, int] = {}
        for h in hits:
            key = f"{h.category}:{h.rule}"
            cats[key] = cats.get(key, 0) + 1
        if policy.on_hit == "mask":
            return Verdict("mask", rt.text, rm.text, reason="hit", categories=cats)
        return Verdict("block", "", "", reason="hit", categories=cats)
