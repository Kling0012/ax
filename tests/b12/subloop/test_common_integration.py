"""Real B12-1 Engine through fake ledger/control/git/deploy ports."""
from types import SimpleNamespace


from core.intake.dest.b12 import common
from core.intake.dest.b12.common.store import Store as CommonStore
from core.intake.dest.b12.subloop import register
from core.intake.dest.b12.subloop.bridge import CommonBridge, bind_events
from tests.b12.subloop.test_creation import OPERATE, request
from tests.b12.subloop.test_bridge import FakeCommon


class Snapshot:
    def __init__(self, root):
        self.stub = FakeCommon(root)
        self.checkout = root
        self.sha = "candidate-sha"
        self.calls = []

    def git(self, *args):
        return self.stub.git(*args)

    def snapshot(self, branch):
        self.calls.append(("snapshot", branch))
        return self.stub.sha

    def verify(self, sha):
        self.calls.append(("verify", sha))
        return sha == "registered-sha"

    def merge(self, sha):
        self.calls.append(("merge", sha))
        return sha

    def tag(self, sha):
        self.calls.append(("tag", sha))


def test_common_real_engine_submit_snapshot_verify_tag_deploy(world):
    initial = world.candidate("new-one")
    world.branches.clear()
    backend = Snapshot(initial)
    deploys, posts, transitions, todos = [], [], [], []
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(transport=None, dest_handlers={})))
    store = CommonStore(world.tmp / "common.sqlite3")
    engine = common.Engine(app, store=store, backend=backend, deploy=lambda key: deploys.append(key))
    app.state.b12 = engine
    engine.ledger = SimpleNamespace(create_todo=lambda todo: todos.append(todo),
                                   get_todo=lambda key: next(todo for todo in todos if todo["id"] == key),
                                   transition=lambda *args, **kwargs: transitions.append((args, kwargs)))
    engine.control = SimpleNamespace(post=lambda path, body: posts.append((path, body)) or {"ok": True})
    bridge = CommonBridge(app, validate=world.validate, template_root=world.repo / "templates/subloop")
    world.service.ports.submit_item = bridge.submit_item
    world.service.ports.accept_branch = bridge.accept_branch
    world.service.stage = bridge.plan_registration
    app.state.b12_subloop = world.service
    app.state.b12_subloop_decode = lambda payload: request(spec=payload["spec"], quote=payload["quote"])
    try:
        register(app)
        # register again must not nest the subscription wrapper.
        bound = engine.event
        register(app)
        assert engine.event is bound
        status, result = app.state.intake.dest_handlers["subloop.create"](
            app, {}, {"idempotency_key": "one", "payload": {"spec": request().spec, "quote": request().quote}}, OPERATE)
        assert status == 202 and result["state"] == "queued", world.parks
        assert len(todos) == 1 and posts[0][0] == "/v1/plugins/work-tdd/items"
        assert posts[0][1]["kind"] == "subloop_create"
        world.branches["subloop/new-one"] = initial
        engine.event(world.completed("one"))
        assert backend.calls == [("snapshot", "subloop/new-one"), ("verify", "registered-sha"),
                                 ("merge", "registered-sha"), ("tag", "registered-sha")]
        assert deploys == ["todo-one"]
        assert common.engine(app).record("todo-one")["phase"] == "done"
        assert world.store.get("one")["phase"] == "registered"
        assert world.events[-1]["kind"] == "plugin.registered"
        assert world.cards[-1][1]["waiting_reason"] == "range_review"
        engine.event(world.completed("one"))
        assert deploys == ["todo-one"]
    finally:
        store.close()


def test_binding_forwards_other_subloop_and_work_events(world):
    forwarded = []
    class Db:
        def get(self, key):
            return {"item": {"kind": "work"}} if key == "item:work" else None
    engine = SimpleNamespace(db=Db(), event=lambda value: forwarded.append(value))
    api = SimpleNamespace(engine=lambda app: engine)
    bind_events(object(), world.service, common=api)
    values = [{"kind": "item.state", "data": {"item_id": "work"}},
              {"kind": "task.state", "data": {"todo_id": "other"}},
              {"kind": "item.state", "data": {"item_id": "other"}}]
    for value in values:
        engine.event(value)
    assert forwarded == values
