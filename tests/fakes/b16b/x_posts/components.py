"""In-process boundaries for X; no socket, process or credential access."""
from __future__ import annotations

from copy import deepcopy

from axcommon import schemas
from core.intake.dest.b16b.common import TargetRegistry
from x_posts import Draft, EnvResult, Inspection


class Events:
    def __init__(self):
        self.records = []
        self.on_emit = None

    def emit(self, kind, subject, data, **kwargs):
        if kind == "automation.delivery":
            schemas.validate("ext/b16b/automation-delivery", data)
        self.records.append((kind, subject, deepcopy(data)))
        if self.on_emit:
            self.on_emit(kind, data)


class Worker:
    def __init__(self):
        self.calls = []
        self.proposals = []
        self.error = None

    def generate(self, target, period, statistics, materials):
        self.calls.append((target, period, deepcopy(statistics), deepcopy(materials)))
        if self.error:
            raise self.error
        return self.proposals


class Inspector:
    def __init__(self):
        self.by_text = {}
        self.calls = []
        self.on_inspect = None

    def inspect(self, target, draft, phase):
        self.calls.append((draft.draft_id, phase))
        if self.on_inspect:
            self.on_inspect(phase)
        value = self.by_text.get((phase, draft.text), Inspection(*(["passed"] * 4)))
        if isinstance(value, Exception):
            raise value
        return value


class Environment:
    def __init__(self):
        self.route = "profile-x-route-a"
        self.calls = []
        self.responses = []
        self.results = {}
        self.available = True
        self.on_post = None
        self.collection_error = None

    def run(self, request, artifacts):
        self.calls.append((self.route, deepcopy(request), deepcopy(artifacts)))
        assert request["requires"] == ["x-session"]
        assert request["secrets"] == []
        assert request["scope"] == "one"
        if not self.available:
            return EnvResult("rejected", self.route, definitely_not_sent=True)
        entry = request["entry"]
        if entry["name"] == "x-collect":
            if self.collection_error:
                raise self.collection_error
            assert not request["irreversible"]
            assert not artifacts
            return EnvResult("succeeded", self.route, {"views": 12} if entry["args"]["op"] == "stats" else {"items": []})
        assert entry["name"] == "x-post"
        assert entry["args"] == {"text": "text.txt", "images": [k for k in artifacts if k != "text.txt"]}
        assert request["irreversible"]
        if self.on_post:
            self.on_post(request)
        response = self.responses.pop(0) if self.responses else EnvResult(
            "succeeded", self.route, {"url": "https://x.com/demo_account/status/12345"})
        if isinstance(response, BaseException):
            raise response
        self.results[request["job_id"]] = response
        return response

    def lookup(self, job_id):
        return self.results.get(job_id, EnvResult("lost", self.route))

    @property
    def posts(self):
        return [call for call in self.calls if call[1]["entry"]["name"] == "x-post"]


class ControlClient:
    """Transport bridge to a real, locally constructed B3 service."""
    def __init__(self, service):
        self.service = service

    def effective(self, plugin_id):
        return self.service.effective(plugin_id)

    def get(self, path, **kwargs):
        if path == "/v1/plugins/x-posts":
            return self.service.plugin_view("x-posts")
        if path == "/v1/commands":
            return {"commands": self.service.list_commands(**kwargs.get("params", {}))}
        if path == "/v1/permissions":
            return {"permissions": self.service.list_permissions()}
        raise AssertionError(path)

    def post(self, path, body):
        assert path == "/v1/permissions/check"
        return self.service.check_permission(body)


def create_control(path, target, events):
    from core.control.service import ControlService
    from core.control.store import Store
    from core.intake.dest.b16b.common import install_control

    from axcommon import timeutil
    store = Store(path)
    store.upsert_plugin({"id": "x-posts", "kind": "subloop", "registered_at": timeutil.now_ts(),
                         "path": "plugins/timer/x-posts/manifest.yaml",
                         "manifest": {"id": "x-posts", "kind": "subloop", "outputs": ["x.post"],
                                      "capabilities": ["run", "collect_only", "draining"], "knobs": {}}})
    service = ControlService(store, events, applier_transport=lambda *args: (200, {}, b'{}'))
    registry = TargetRegistry([target.delivery_target()])
    install_control(service, lambda: registry)
    return service, registry
