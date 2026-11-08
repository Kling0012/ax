"""エラーの分類（S-2.3、D-2.2 axcommon.errors）。表は config/error-rules.yaml。

classify() は表を上から順に見て、最初に当たった規則を返す。どれにも当たらなければ unknown / unclassified（管理役の受信対象。S-10）。
error_event_data() は時系列記録の `error.classified` の data を作る（message は短く、秘密を入れない。呼び出し側が redact を通す）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .defaults import config_dir

MESSAGE_MAX = 300


@dataclass(frozen=True)
class Classification:
    cls: str  # system.expected | artifact.bug | unknown
    code: str
    counts_as_failure: str  # yes | no | hold
    action: str = ""

    @property
    def retryable(self) -> bool:
        return self.cls == "system.expected" and self.code in ("rate_limit", "timeout", "network")


UNKNOWN = Classification("unknown", "unclassified", "hold", "管理役の受信対象（S-10）")


@dataclass
class Rule:
    cls: str
    code: str
    match: dict
    counts_as_failure: str
    action: str = ""
    _re: Any = field(default=None, repr=False)

    def matches(self, *, http_status=None, body=None, exception=None, exit_code=None, hint=None) -> bool:
        m = self.match
        if not m:
            return False
        if "hint" in m and hint not in m["hint"]:
            return False
        if "http_status" in m and http_status not in m["http_status"]:
            return False
        if "http_status_range" in m:
            lo, hi = m["http_status_range"]
            if http_status is None or not (lo <= http_status <= hi):
                return False
        if "body_regex" in m and (body is None or self._re.search(body) is None):
            return False
        if "exception" in m and exception not in m["exception"]:
            return False
        if "exit_code" in m and exit_code not in m["exit_code"]:
            return False
        return True


class ErrorRules:
    def __init__(self, rules: list[Rule]):
        self.rules = rules

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "ErrorRules":
        import yaml

        p = Path(path) if path else config_dir() / "error-rules.yaml"
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        from . import schemas

        schemas.validate("error-rules", doc)
        rules = []
        for r in doc["rules"]:
            rule = Rule(r["class"], r["code"], r["match"], r["counts_as_failure"], r.get("action", ""))
            if "body_regex" in rule.match:
                rule._re = re.compile(rule.match["body_regex"], re.IGNORECASE)
            rules.append(rule)
        return cls(rules)

    def classify(self, *, http_status: int | None = None, body: str | None = None, exception: BaseException | str | None = None,
                 exit_code: int | None = None, hint: str | None = None) -> Classification:
        exc_name = None
        if exception is not None:
            exc_name = exception if isinstance(exception, str) else type(exception).__name__
        for r in self.rules:
            if r.matches(http_status=http_status, body=body, exception=exc_name, exit_code=exit_code, hint=hint):
                return Classification(r.cls, r.code, r.counts_as_failure, r.action)
        return UNKNOWN

    def classify_exception(self, exc: BaseException) -> Classification:
        """例外の型名（と、例外の連鎖の原因の型名）で分類する。"""
        seen = 0
        cur: BaseException | None = exc
        while cur is not None and seen < 5:
            c = self.classify(exception=cur, body=str(cur))
            if c is not UNKNOWN:
                return c
            cur = cur.__cause__ or cur.__context__
            seen += 1
        return UNKNOWN


def error_event_data(c: Classification, message: str = "") -> dict:
    """`error.classified` の data（S-3.3: class、code、message（短く））。"""
    return {"class": c.cls, "code": c.code, "message": message[:MESSAGE_MAX]}
