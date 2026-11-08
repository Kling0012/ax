"""Deterministic outbox inspection; model output never supplies executable commands."""
from __future__ import annotations

import json
import re
from dataclasses import fields
from datetime import timedelta, timezone
from urllib.parse import urlsplit

from axcommon import timeutil
from .model import ACTIONS, PERMISSION_ACTIONS, URL_PREFIXES, Candidate

URL = re.compile(r"(?:[a-zA-Z][a-zA-Z0-9+.-]*://|www\.)[^\s<>\"']*")
FORBIDDEN_REPLY = re.compile(
    r"\b(identity|verify|verification|passport|payment|pay|money|bank|wallet|invoice|"
    r"commission|order|purchase|promise|commitment|personal\s+(?:data|information)|"
    r"address|phone|contract|hire)\b|本人確認|支払|お金|受注|発注|約束|個人情報|支付|转账|身份|承诺",
    re.IGNORECASE,
)
JST = timezone(timedelta(hours=9))


def decode_line(line):
    """Internal adapter helper; unknown fields are discarded before processing."""
    row = json.loads(line)
    if not isinstance(row, dict):
        raise ValueError("object_required")
    names = {field.name for field in fields(Candidate)}
    if "action" not in row:
        raise ValueError("action_required")
    return Candidate(**{name: value for name, value in row.items() if name in names})


class OutboxCheck:
    def __init__(self, redactor, public_gate, settings):
        self.redactor, self.public_gate, self.settings = redactor, public_gate, settings

    def shape(self, candidate):
        if not isinstance(candidate, Candidate):
            return "invalid_row"
        if any(not isinstance(value, str) for value in candidate.payload().values()):
            return "invalid_field"
        if candidate.action not in ACTIONS:
            return "action_not_allowed"
        if candidate.action not in PERMISSION_ACTIONS:
            return "operation_unresolved"
        if candidate.action in ("publish", "reply", "respond") and not candidate.text.strip():
            return "body_required"
        required = {"reply": candidate.conv_id, "respond": candidate.item_id,
                    "friend_request": candidate.target, "friend_accept": candidate.target}
        if candidate.action in required and not required[candidate.action].strip():
            return "target_required"
        if len(candidate.body) > (1200 if candidate.action == "publish" else 800):
            return "length"
        if self.redactor.contains_secret(json.dumps(candidate.payload(), ensure_ascii=False)):
            return "secret_or_pii"
        urls = URL.findall(" ".join((candidate.text, candidate.note)))
        if candidate.url:
            urls.append(candidate.url)
        for url in urls:
            try:
                parsed = urlsplit(url)
            except ValueError:
                return "url_not_allowed"
            if (not url.startswith(URL_PREFIXES) or parsed.username or parsed.password
                    or parsed.scheme != "https" or "\\" in url):
                return "url_not_allowed"
            # Encoded separators and parent segments must not escape the approved prefix.
            from urllib.parse import unquote
            path = unquote(parsed.path)
            if any(segment in (".", "..") for segment in path.split("/")) or "\\" in path:
                return "url_not_allowed"
        if candidate.action == "reply" and FORBIDDEN_REPLY.search(candidate.text):
            return "forbidden_reply_topic"
        if not self.redactor.index_ok or not self.redactor.pii_list_ok:
            return "inspection_unavailable"
        try:
            # Gate is called for every outgoing text, including friend-request notes.
            if self.public_gate(candidate) is not True:
                return "public_gate"
        except Exception:
            return "public_gate_unavailable"
        return None

    def history_check(self, candidate, history, pending, now):
        recent = [row for row in history if row["state"] in ("sent", "unknown", "launching")]
        sent = [row for row in history if row["state"] == "sent"]
        payloads = [row["payload"] for row in recent] + [value.payload() for value in pending]
        if candidate.action == "publish" and any(
                row["action"] == "publish" and row["text"] == candidate.text for row in payloads):
            return "duplicate_publish"
        if candidate.action == "respond" and any(
                row["action"] == "respond" and row["item_id"] == candidate.item_id for row in payloads):
            return "duplicate_respond"
        if candidate.action == "reply":
            if any(row.action == "reply" and row.conv_id == candidate.conv_id for row in pending):
                return "reply_24h"
            if any(row["payload"]["action"] == "reply" and row["payload"]["conv_id"] == candidate.conv_id
                   and (row["state"] != "sent" or now - timeutil.parse_ts(row["checked_at"]) < timedelta(hours=24))
                   for row in recent):
                return "reply_24h"
            today = now.astimezone(JST).date()
            count = sum(row["operation"] == "reply"
                        and timeutil.parse_ts(row["checked_at"]).astimezone(JST).date() == today for row in sent)
            count += sum(row.action == "reply" for row in pending)
            if count >= self.settings.reply_cap_day:
                return "day_cap"
        if len(pending) >= self.settings.run_send_max:
            return "run_cap"
        return None

    def inspect(self, candidates, history, now):
        approved, rejected = [], []
        for index, candidate in enumerate(candidates):
            reason = self.shape(candidate)
            if reason is None:
                reason = self.history_check(candidate, history, approved, now)
            if reason:
                # Do not retain uninspected output, secrets, or model-provided exceptions.
                rejected.append({"index": index, "reason": reason})
            else:
                approved.append(candidate)
        return approved, rejected
