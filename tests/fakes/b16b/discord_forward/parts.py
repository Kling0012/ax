"""Network-free X source, control API and Discord transport."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 7, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class Source:
    def __init__(self):
        self.posts, self.calls = [], []
        self.fail = False

    def fetch(self, target):
        self.calls.append(target.id)
        if self.fail:
            raise RuntimeError("source_unavailable")
        return list(self.posts)


class Control:
    def __init__(self):
        self.enabled, self.events, self.calls = True, [], []

    def post(self, path, body):
        assert path == "/v1/permissions/check"
        self.calls.append(dict(body))
        self.events.append({"kind": "permission.checked", "result": "allowed" if self.enabled else "denied"})
        return {"allowed": self.enabled}


class WebhookTransport:
    def __init__(self):
        self.channel_id = "12345"
        self.calls, self.outcomes = [], []
        self.after_get = None
        self.before_post = None

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, body))
        if method == "GET":
            if self.after_get:
                self.after_get()
            return 200, {}, json.dumps({"channel_id": self.channel_id}).encode()
        assert method == "POST" and url.endswith("?wait=true")
        if self.before_post:
            self.before_post()
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, int):
            return outcome, {}, b"{}"
        if outcome == "no_id":
            return 200, {}, b"{}"
        return 200, {}, json.dumps({"channel_id": self.channel_id, "id": "98765"}).encode()

    @property
    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]


class EnvRunner:
    def __init__(self, artifact):
        self.artifact, self.calls = artifact, []

    def run(self, request, artifacts):
        self.calls.append((request, artifacts))
        return SimpleNamespace(state="succeeded", output={"posts.json": self.artifact})
