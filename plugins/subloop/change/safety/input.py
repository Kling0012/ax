"""S-19.3 input preparation and rolling admission; no external operations."""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import re
import unicodedata

from axcommon.defaults import Defaults


def timestamp(value) -> datetime:
    value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


class _VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.output = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        style = re.sub(r"\s+", "", (attrs.get("style") or "").lower())
        hidden = (tag in {"script", "style", "template", "head"}
                  or "hidden" in attrs or (attrs.get("aria-hidden") or "").lower() == "true"
                  or any(s in style for s in ("display:none", "visibility:hidden", "opacity:0", "font-size:0")))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, hidden or any(h for _, h in self.stack)))

    def handle_startendtag(self, tag, attrs):
        # Self-closing elements cannot hide subsequent visible text.
        pass

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                self.stack = self.stack[:i]
                return

    def handle_data(self, data):
        if not any(hidden for _, hidden in self.stack):
            self.output.append(data)


def sanitize(text: str) -> tuple[str, bool]:
    parser = _VisibleHTML()
    parser.feed(text)
    parser.close()
    visible = "".join(parser.output)
    cleaned = "".join(c for c in visible
                      if (unicodedata.category(c) not in {"Cf", "Cc", "Cs"} or c in "\n\r\t")
                      and c not in "͏⠀ㅤﾠ"
                      and not 0xFE00 <= ord(c) <= 0xFE0F
                      and not 0xE0100 <= ord(c) <= 0xE01EF)
    return cleaned, cleaned != text


@dataclass(frozen=True)
class PreparedInput:
    state: str
    body: str | None
    diff: str | None
    removed: dict[str, bool]
    reason: str | None = None


def prepare_input(body: str, diff: str = "", *, max_bytes: int | None = None) -> PreparedInput:
    limit = Defaults.load().get("change_input_max_bytes") if max_bytes is None else max_bytes
    if type(limit) is not int or limit < 1:
        raise ValueError("positive byte limit required")
    # Check the complete original UTF-8 input, before any stripping.
    for name, text in (("body", body), ("diff", diff)):
        if not isinstance(text, str):
            return PreparedInput("parked", None, None, {}, "invalid_" + name)
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            return PreparedInput("parked", None, None, {}, "invalid_utf8_" + name)
        if size > limit:
            return PreparedInput("parked", None, None, {}, name + "_too_large")
    clean_body, body_removed = sanitize(body)
    clean_diff, diff_removed = sanitize(diff)
    return PreparedInput("ready", clean_body, clean_diff, {"body": body_removed, "diff": diff_removed})


class AdmissionGate:
    """Store.transact(callback) must serialize read/decision/reservation atomically.

    The store supplies a mutable list of accepted records persisted on success.
    Reuse the collection/ledger transaction; this module creates no private DB.
    """
    def __init__(self, store, defaults: Defaults | None = None):
        self.store = store
        self.defaults = defaults or Defaults.load()

    def reserve(self, *, key: str, author: str, kind: str, now) -> dict:
        if kind not in {"issue", "pr", "comment"} or not key or not author:
            raise ValueError("invalid admission record")
        author = author.casefold()
        now = timestamp(now)

        def decide(records):
            if any(r["key"] == key for r in records):
                return {"state": "ready", "reserved": False}
            active = [r for r in records if now - timedelta(hours=24) < timestamp(r["at"]) <= now]
            author_kind = [r for r in active if r["author"] == author and
                           (r["kind"] == "comment") == (kind == "comment")]
            author_limit = self.defaults.get("change_author_comments_per_day" if kind == "comment" else "change_author_items_per_day")
            if len(author_kind) >= author_limit:
                return {"state": "queued", "reason": "author_limit", "check_at": (min(timestamp(r["at"]) for r in author_kind) + timedelta(hours=24)).isoformat()}
            items = [r for r in active if r["kind"] != "comment"]
            if kind != "comment" and len(items) >= self.defaults.get("change_items_per_day"):
                return {"state": "queued", "reason": "global_limit", "check_at": (min(timestamp(r["at"]) for r in items) + timedelta(hours=24)).isoformat()}
            records.append({"key": key, "author": author, "kind": kind, "at": now.isoformat()})
            return {"state": "ready", "reserved": True}

        return self.store.transact(decide)


def classification_key(body_revision, comments: list[dict], policy_revision) -> tuple:
    return (body_revision, tuple(sorted((str(c["comment_id"]), str(c["revision"])) for c in comments)), policy_revision)


class ClassificationMemo:
    """Input-bound real-environment preclassification cache (R-A12).

    records is a checkpoint-owned mapping. complete applies the classifier's
    full schema, supplied by the S-8 adapter. Unknown remains unknown; this
    cache never converts inability to classify into 'verification unnecessary'.
    """
    def __init__(self, records=None):
        self.records = records if records is not None else {}

    def resolve(self, *, body_revision, comments, policy_revision, classify, complete):
        import json
        key = json.dumps(classification_key(body_revision, comments, policy_revision), ensure_ascii=False)
        if key in self.records:
            try:
                if complete(self.records[key]) is True:
                    return deepcopy(self.records[key])
            except Exception:
                pass
        try:
            result = classify()
            if complete(result) is True:
                self.records[key] = deepcopy(result)
                return deepcopy(result)
        except Exception:
            pass
        return None
