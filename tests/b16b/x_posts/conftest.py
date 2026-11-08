from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

PACKAGE = Path(__file__).resolve().parents[3] / "plugins" / "timer" / "x-posts"
spec = importlib.util.spec_from_file_location("x_posts", PACKAGE / "__init__.py",
                                              submodule_search_locations=[str(PACKAGE)])
module = importlib.util.module_from_spec(spec)
sys.modules["x_posts"] = module
spec.loader.exec_module(module)

from axcommon import ids, timeutil
from x_posts import ControlAdapter, Draft, Target, XEnvironment, XPosts
from tests.fakes.b16b.x_posts.components import (
    ControlClient, Environment, Events, Inspector, Worker, create_control,
)


@pytest.fixture
def rig(tmp_path, fake_clock):
    target = Target("x-posts", "sample", "sample/repo", "demo-x-destination", "demo_account")
    events, env, worker, inspector = Events(), Environment(), Worker(), Inspector()
    service, registry = create_control(tmp_path / "control.sqlite3", target, events)
    control = ControlAdapter(ControlClient(service), lambda: registry)
    lost = []
    engines = []
    directory = tmp_path / "ax-x-state"

    def engine():
        value = XPosts(directory, control=control, environment=XEnvironment(env), worker=worker,
                       inspector=inspector, events=events, lost=lost.append)
        engines.append(value)
        return value

    def permission():
        return service.create_permission({"project": target.project, "repo": target.repo,
            "destination": target.destination, "operations": ["投稿"], "conditions": {},
            "source_instruction": ids.new_ulid()}, by="user")

    def start(p=None):
        p = p or permission()
        service.issue({"target": "x-posts", "op": "set_mode",
            "args": {"mode": "run", "permission_ids": [p["id"]]}, "corr": p["source_instruction"],
            "reason": "試験用の開始指示"}, issued_by="user")
        return p

    def drafts(*texts):
        worker.proposals = [Draft(f"draft-{n}", text, timeutil.now_ts()) for n, text in enumerate(texts)]
        result = engines[-1].collect(target, "2026-11-05", scheduled_at=timeutil.now_ts())
        assert result["result"] == "drafted", result
        return engines[-1].store.candidates(target)

    first = engine()
    yield SimpleNamespace(target=target, events=events, env=env, worker=worker, inspector=inspector,
        service=service, registry=registry, control=control, lost=lost, engine=first, reopen=engine,
        clock=fake_clock, directory=directory, permission=permission, start=start, drafts=drafts)
    for value in engines:
        value.close()
    service.store.close()
