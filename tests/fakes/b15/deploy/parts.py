"""ネットワークを使わない env.run の偽。"""
from __future__ import annotations

from copy import deepcopy


class FakeEnvRun:
    def __init__(self, result):
        self.result = deepcopy(result)
        self.requests = []

    def __call__(self, request):
        self.requests.append(deepcopy(request))
        return deepcopy(self.result)
