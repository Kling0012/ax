"""T-38: exercise dispatch through collection using only injected services."""
from plugins.nodes.ci.public_ci import PublicCI


class Store(dict):
    def put(self, key, value):
        self[key] = value


class Fake:
    def __init__(self):
        self.dispatches = []
        self.published = []
        self.saved = []
        self.attempt = 1
        self.missing = False
        self.fail = False
        self.sha = "a" * 40

    def dispatch(self, repo, workflow, ref, inputs):
        self.dispatches.append((repo, workflow, ref, inputs))

    def runs(self, repo, workflow):
        return [{"id": n, "display_title": f"ci {self.sha} full", "head_branch": "main",
                 "created_at": "1970-01-01T00:00:00Z"} for n in (42, 41)]

    def run(self, repo, rid):
        return {**next(r for r in self.runs(repo, "ci.yml") if r["id"] == rid),
                "run_attempt": self.attempt, "status": "completed", "conclusion": "success",
                "html_url": f"https://example.invalid/runs/{rid}"}

    def jobs(self, repo, rid, attempt):
        return [{"name": "plan", "conclusion": "success"},
                *[{"name": f"shard {n}", "conclusion": "success"} for n in range(1, 257)]]

    def artifacts(self, repo, rid):
        a = self.attempt
        names = [f"plan-{a}"] + [f"shard-{n}-{a}" for n in range(1, 257)]
        if self.missing:
            names.remove(f"shard-256-{a}")
        return [{"name": n} for n in names] + [{"name": f"shard-999-{a}"}]

    def collect_artifact(self, request, rid, attempt, name, artifact):
        if self.fail:
            raise OSError("collection failed")
        assert artifact["name"] == name
        self.saved.append((request["private_sha"], rid, attempt, name))

    def statuses(self, repo, sha):
        return list(reversed(self.published))

    def set_status(self, repo, sha, state, context, description, target_url):
        self.published.append(dict(repo=repo, sha=sha, state=state, context=context,
                                   description=description, target_url=target_url))


def test_dispatch_collection_retry_and_new_attempt():
    clock = [0]
    fake, store = Fake(), Store()
    ci = PublicCI(fake, store, lambda: clock[0])
    request = ci.trigger("private/project", "public/project", fake.sha, "full")
    assert request["run_id"] == 41
    assert fake.dispatches == [("public/project", "ci.yml", "main", {"sha": fake.sha, "mode": "full"})]
    assert ci.trigger("private/project", "public/project", fake.sha, "full") == request
    fake.missing = True
    clock[0] = 4000
    assert ci.collect(request)["collected"] is False
    assert fake.published == []
    clock[0] += 60
    assert ci.collect(request)["next_check"] == 4180
    fake.missing = False
    fake.fail = True
    clock[0] = 4180
    assert not ci.collect(request)["collected"]
    assert fake.published == []
    fake.fail = False
    clock[0] = 4420
    result = ci.collect(request)
    assert result["collected"] and result["attempt"] == 1
    assert len(fake.saved) == 257 and len(fake.published) == 1
    assert all(x[:3] == (fake.sha, 41, 1) and "999" not in x[3] for x in fake.saved)
    fake.attempt = 2
    result = ci.collect(request)
    assert result["collected"] and result["attempt"] == 2
    assert len(fake.saved) == 514
    assert fake.published[0]["sha"] == fake.sha and fake.published[0]["description"] == "full: success"


def test_mismatched_run_and_full_status_do_not_publish_quick():
    clock = [0]
    fake, store = Fake(), Store()
    ci = PublicCI(fake, store, lambda: clock[0])
    request = ci.trigger("private/project", "public/project", fake.sha, "full")
    fake.sha = "b" * 40
    assert ci.collect(request)["reason"] == "run_mismatch"
    assert fake.published == [] and fake.saved == []
    fake.sha = "a" * 40
    fake.published.append(dict(context="ci/public", state="pending", description="full: pending"))
    quick = {**request, "mode": "quick"}
    original_run = fake.run
    fake.run = lambda repo, rid: {**original_run(repo, rid), "display_title": f"ci {fake.sha} quick"}
    assert not ci.collect(quick)["collected"]
    assert len(fake.published) == 1


def test_node_dispatch_and_collect_with_injected_service():
    import asyncio
    from types import SimpleNamespace
    from plugins.nodes.ci import trigger, collect

    fake = Fake()
    service = PublicCI(fake, Store(), lambda: 0)

    class Context:
        svc = SimpleNamespace(public_ci=service, plugin_id="test")
        item_id, node_id = "item", "node"
        launched = None

        async def launching(self, key, ref):
            self.launched = (key, ref)

        async def call(self, fn, *args, **kwargs):
            return fn(*args, **kwargs)

    async def scenario():
        ctx = Context()
        output = await trigger.run(ctx, {"with": {"repo": "public/project", "workflow": "ci.yml", "ref": "main",
                                                    "private_repo": "private/project", "private_sha": fake.sha, "mode": "full"}})
        assert output["status"] == "ok" and ctx.launched[1]["public_ci"]
        result = await collect.run(ctx, {"with": {"repo": "public/project", "request": output["data"]}})
        assert result["status"] == "ok" and result["data"]["attempt"] == 1

    asyncio.run(scenario())
