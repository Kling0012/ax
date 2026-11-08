"""Deterministic model, quota-store, and injection fakes; no network."""
from copy import deepcopy
from collections import deque
from threading import RLock

from plugins.subloop.change.safety import Candidate


class Model:
    def __init__(self, *replies):
        self.replies = deque(replies)
        self.requests = []

    def __call__(self, request):
        self.requests.append(deepcopy(request))
        result = self.replies.popleft() if self.replies else None
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    def candidate(self, identity="model-a", tier="standard"):
        return Candidate(identity, {"id": identity, "kind": "model", "tier": tier}, self)


class Store:
    def __init__(self):
        self.records = []
        self.lock = RLock()

    def transact(self, callback):
        with self.lock:
            return callback(self.records)


def safe(summary="Observed behavior needs correction.", concerns=()):
    return {"verdict": "safe", "concerns": list(concerns), "summary": summary}


class InjectionCheck:
    def __init__(self, result=True):
        self.result = result
        self.requests = []

    def __call__(self, summary):
        self.requests.append(deepcopy(summary))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result
