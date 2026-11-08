from __future__ import annotations

import builtins
import os
from pathlib import Path

import pytest

from axcommon import timeutil
from x_posts import Draft, EnvResult, UnboundEnvironment, XEnvironment


@pytest.mark.parametrize("route", ["profile-x-route-a", "profile-x-route-b"])
def test_t106_collection_and_post_share_selected_x_session_route(rig, route):
    rig.env.route = route
    rig.start()
    rig.drafts("demo")
    assert rig.engine.send_one(rig.target)["state"] == "sent"
    assert {call[0] for call in rig.env.calls} == {route}
    assert {call[1]["entry"]["name"] for call in rig.env.calls} == {"x-collect", "x-post"}
    assert all(call[1]["requires"] == ["x-session"] for call in rig.env.calls)
    assert all(call[1]["secrets"] == [] for call in rig.env.calls)
    assert "cookie" not in repr(rig.worker.calls).lower()
    assert "reply-to" not in repr(rig.env.calls)


def test_t106_missing_credentials_park_only_x_without_fallback(rig):
    rig.start()
    rig.env.available = False
    result = rig.engine.collect(rig.target, "day", scheduled_at=timeutil.now_ts())
    assert result["reason"] == "collection_failed"
    assert rig.worker.calls == []
    assert {call[0] for call in rig.env.calls} == {"profile-x-route-a"}
    assert rig.service.effective("x-posts")["mode"] == "run"
    assert rig.engine.inventory()
    assert not rig.env.posts


@pytest.mark.parametrize("args", [{"query": "not-a-hashtag"}, {"query": "#two words"}, {"limit": 0}, {"limit": True}, {"limit": 201}])
def test_t106_bad_collection_arguments_never_reach_environment(rig, args):
    with pytest.raises(ValueError):
        rig.engine.environment.collect("sample-job", **args)
    assert rig.env.calls == []


@pytest.mark.parametrize("names", [("../1.png",), ("1.mp4", "1.png"), ("1.png", "1.png"), ("reply.txt",)])
def test_t106_post_cannot_smuggle_files_or_reply_arguments(rig, names):
    draft = Draft("draft", "text", timeutil.now_ts(), names, {name: b"demo" for name in names})
    with pytest.raises(ValueError):
        rig.engine.environment.post("sample-job", draft)
    assert rig.env.calls == []


def test_t106_unbound_transport_is_explicit(rig):
    with pytest.raises(NotImplementedError):
        XEnvironment(UnboundEnvironment()).collect("sample-job")


def test_t155_boot_ignores_all_legacy_paths_and_id_collision(rig, tmp_path, monkeypatch):
    legacy = tmp_path / "old-system"
    paths = ["queue/20261105-2100-blog/text.txt", "posted/item/text.txt", "held/item/reason.txt",
             "results.tsv", ".local/state/x2discord/cursor.json"]
    for name in paths:
        path = legacy / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("legacy material", encoding="utf-8")
    rig.start()
    row = rig.drafts("new AX text")[0]
    collision = legacy / "queue" / row["id"]
    collision.mkdir()
    (collision / "text.txt").write_text("old unrelated candidate", encoding="utf-8")
    touched = []

    def forbid_legacy(original):
        def wrapped(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)) and str(legacy) in os.fsdecode(file):
                touched.append(os.fsdecode(file))
                raise AssertionError("legacy paths must never be read")
            return original(file, *args, **kwargs)
        return wrapped

    monkeypatch.setattr(builtins, "open", forbid_legacy(builtins.open))
    monkeypatch.setattr(os, "open", forbid_legacy(os.open))
    monkeypatch.setattr(Path, "read_text", forbid_legacy(Path.read_text))
    monkeypatch.setattr(Path, "iterdir", forbid_legacy(Path.iterdir))
    reopened = rig.reopen()
    assert reopened.recover()
    assert reopened.send_one(rig.target, candidate_id="20261105-2100-blog")["state"] == "denied"
    assert reopened.send_one(rig.target)["state"] == "sent"
    assert reopened.send_one(rig.target)["state"] == "skipped"
    assert row["id"].startswith("ax-x-")
    assert len(rig.env.posts) == 1
    assert rig.env.posts[0][2]["text.txt"] == b"new AX text"
    assert touched == []


def test_t155_old_unattempted_ax_candidate_is_excluded_but_failed_attempt_survives(rig):
    rig.start()
    rows = rig.drafts("failed", "never-attempted")
    rig.env.responses = [EnvResult("failed", definitely_not_sent=True)]
    assert rig.engine.send_one(rig.target, candidate_id=rows[0]["id"])["state"] == "failed"
    rig.clock.advance(1)
    reopened = rig.reopen()
    assert reopened.recover()
    assert reopened.send_one(rig.target, candidate_id=rows[1]["id"])["state"] == "skipped"
    assert reopened.send_one(rig.target, candidate_id=rows[0]["id"])["state"] == "sent"
    assert len(rig.env.posts) == 2
