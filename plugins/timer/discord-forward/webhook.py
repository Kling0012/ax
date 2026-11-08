"""Discord webhook verification and single-attempt delivery.

    transport(method, url, headers, body, timeout) -> (status, headers, bytes)
    has the same shape as axcommon.client.urllib_transport. No automatic retry.
"""
from __future__ import annotations

import json
import re
import threading
from urllib.error import HTTPError
from urllib.parse import urlsplit

from axcommon.client import urllib_transport
from axcommon.creds import read_secret
from core.intake.dest.b16b.delivery import NotSent


class DiscordWebhook:
    def __init__(self, *, transport=urllib_transport, secrets=read_secret, timeout=15):
        self.transport, self.secrets, self.timeout = transport, secrets, timeout
        self._verified = threading.local()

    def _url(self, target):
        try:
            value = self.secrets(target.secret)
            url = urlsplit(value)
            if (url.scheme != "https" or url.netloc != "discord.com"
                    or url.query or url.fragment
                    or not re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9._-]+", url.path)):
                raise ValueError("invalid_webhook")
            return value
        except Exception:
            raise NotSent("webhook_secret_unavailable") from None

    def _request(self, method, url, body=None):
        try:
            return self.transport(method, url, {"Content-Type": "application/json"}, body, self.timeout)
        except HTTPError as exc:
            # The response status is enough; never retain or print the secret URL/body.
            return exc.code, {}, b""
        except Exception:
            raise RuntimeError("webhook_outcome_unknown") from None

    def channel_matches(self, target):
        self._verified.value = None
        try:
            url = self._url(target)
            status, _, body = self._request("GET", url)
            if status == 200 and json.loads(body).get("channel_id") == target.channel_id:
                self._verified.value = (target.id, target.channel_id, url)
                return True
            return False
        except Exception:
            return False

    def send(self, target, content):
        verified = getattr(self._verified, "value", None)
        self._verified.value = None
        if verified is None or verified[:2] != (target.id, target.channel_id):
            raise NotSent("webhook_not_verified")
        # Use precisely the URL whose channel was verified; do not re-read a rotating secret.
        url = verified[2]
        try:
            payload = json.dumps({"content": content, "allowed_mentions": {"parse": []}},
                                 ensure_ascii=False).encode("utf-8")
        except Exception:
            raise NotSent("invalid_webhook_payload") from None
        status, _, body = self._request("POST", url + "?wait=true", payload)
        if 400 <= status < 500 and status != 408:
            raise NotSent("webhook_rejected")
        if status not in (200, 201):
            raise RuntimeError("webhook_outcome_unknown")
        try:
            receipt = json.loads(body)
            message_id = receipt["id"]
            if (receipt.get("channel_id") != target.channel_id
                    or not isinstance(message_id, str) or not re.fullmatch(r"[0-9]+", message_id)):
                raise ValueError("invalid_receipt")
            return message_id
        except Exception:
            raise RuntimeError("webhook_receipt_invalid") from None
