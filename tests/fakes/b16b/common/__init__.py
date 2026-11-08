"""B16b の偽。ソケットと実在の送信先は使わない。"""
from copy import deepcopy

from core.intake.dest.b16b.delivery import NotSent


class FakeSender:
    def __init__(self, kind):
        self.kind, self.calls, self.outcome = kind, [], "sent"

    def __call__(self):
        self.calls.append(self.kind)
        if self.outcome == "unknown":
            raise TimeoutError("fictional outcome")
        if self.outcome == "failed":
            raise NotSent("fictional not sent")
        if self.outcome == "crash":
            raise SystemExit("fictional process crash")
        if self.outcome == "empty":
            return None
        return f"fake-{self.kind}-{len(self.calls)}"


class FakePermissionReader:
    def __init__(self, control):
        self.control, self.calls = control, []

    def check(self, target, operation):
        body = {k: target[k] for k in ("project", "repo", "destination")}
        body["operation"] = operation
        self.calls.append(deepcopy(body))
        return self.control.check_permission(body)["allowed"]


class FakeEvents:
    def __init__(self):
        self.events, self.cards = [], []

    def emit(self, kind, subject, data, **kwargs):
        self.events.append({"kind": kind, "subject": subject, "data": deepcopy(data)})

    def lost(self, data):
        self.cards.append(deepcopy(data))
