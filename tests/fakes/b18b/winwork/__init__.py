"""Offline Windows executor and timeline doubles."""
from copy import deepcopy
from core.resources.winwork import DesktopState, ExecutionResult


class FakeExecutor:
    def __init__(self):
        self.states = {}
        self.calls = []
        self.probes = []
        self.probe_error = None
        self.run_error = None
        self.on_run = None
        self.result = ExecutionResult("succeeded", {"artifact": "fake-output"})

    def probe(self, tool, location):
        self.probes.append((tool["id"], location))
        if self.probe_error:
            raise self.probe_error
        return self.states.get(tool["id"], DesktopState(True, True, False))

    def run(self, tool, location, request):
        self.calls.append((deepcopy(tool), location, deepcopy(request)))
        if self.on_run:
            self.on_run(tool, request)
        if self.run_error:
            raise self.run_error
        return self.result


class FakeEvents:
    def __init__(self):
        self.events = []

    def emit(self, kind, subject, data, **kw):
        self.events.append({"kind": kind, "subject": subject, "data": deepcopy(data)})
