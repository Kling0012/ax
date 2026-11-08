"""S-12.1/T-51 and S-22.7 secret-slot behavior without service I/O."""
from __future__ import annotations

import os
import stat

import pytest

from core.hq.secrets import SecretFailure, SecretSlots
from tests.fakes.b10b.runtime import Intake


class Sink:
    def __init__(self):
        self.events = []
    def emit(self, kind, subject, data):
        self.events.append((kind, subject, data))


def definitions():
    return {
        "local-key": {"file": "local-key", "operations": ["write"], "max_length": 32, "pattern": r"[A-Za-z0-9]+", "target": {"local": True}},
        "remote-key": {"file": "remote-key", "operations": ["write"], "max_length": 32, "pattern": r"[A-Za-z0-9]+", "target": {"env": "ci", "name": "key"}},
    }


def slots(tmp_path, *, registered=lambda env, name: True, emit=None, intake=None):
    tmp_path.chmod(0o770)
    return SecretSlots(tmp_path, definitions(), registered=registered, emit=emit, intake=intake)


def test_secret_is_write_only_validated_and_never_returned(tmp_path):
    sink = Sink()
    result = slots(tmp_path, emit=sink.emit).store("local-key", "secretvalue")
    assert result == {"stored": True, "slot": "local-key", "push_requested": False}
    assert sink.events == [("secret.stored", "local-key", {"name": "local-key"})]
    listed = slots(tmp_path, emit=sink.emit).metadata()
    assert listed == [{"name": "local-key", "configured": True, "registered": True},
                      {"name": "remote-key", "configured": False, "registered": True}]
    content = (tmp_path / "local-key").read_text()
    assert content == "secretvalue"
    assert all("secretvalue" not in repr(value) for value in (result, sink.events, listed))
    assert stat.S_IMODE((tmp_path / "local-key").stat().st_mode) == 0o640


@pytest.mark.parametrize("slot,value", [("unknown", "secretvalue"), ("../escape", "secretvalue"),
    ("local-key", ""), ("local-key", "x" * 33), ("local-key", "bad-value"), ("remote-key", "secretvalue")])
def test_unknown_unsafe_invalid_and_unregistered_inputs_rejected(tmp_path, slot, value):
    store = slots(tmp_path, registered=lambda env, name: False, emit=Sink().emit)
    with pytest.raises(SecretFailure):
        store.store(slot, value)
    assert not list(tmp_path.iterdir())


def test_secret_input_requires_event_sink_and_remote_push_is_explicit(tmp_path):
    with pytest.raises(SecretFailure, match="event_unavailable"):
        slots(tmp_path).store("local-key", "secretvalue")
    sink, intake = Sink(), Intake()
    result = slots(tmp_path, emit=sink.emit, intake=intake).store("remote-key", "secretvalue", request_id="request-1")
    assert result == {"stored": True, "slot": "remote-key", "push_requested": True}
    assert intake.calls == [("secret.push", {"slot": "remote-key"}, {"idempotency_key": "secret.push:request-1"})]


def test_slot_directory_and_symlink_are_rejected(tmp_path):
    sink = Sink()
    tmp_path.chmod(0o700)
    with pytest.raises(SecretFailure, match="slot_directory_unsafe"):
        SecretSlots(tmp_path, definitions(), emit=sink.emit).store("local-key", "secretvalue")
    tmp_path.chmod(0o770)
    target = tmp_path.parent / "target"
    target.write_text("outside")
    (tmp_path / "local-key").symlink_to(target)
    with pytest.raises(SecretFailure, match="slot_file_unsafe"):
        slots(tmp_path, emit=sink.emit).store("local-key", "secretvalue")
    assert target.read_text() == "outside"


def test_existing_archive_must_be_private_directory(tmp_path):
    sink = Sink()
    (tmp_path / "local-key").write_text("oldvalue")
    target = tmp_path.parent / "outside-archive"
    target.mkdir()
    (tmp_path / "archive").symlink_to(target, target_is_directory=True)
    with pytest.raises(SecretFailure, match="slot_archive_unsafe"):
        slots(tmp_path, emit=sink.emit).store("local-key", "newvalue")
    assert (tmp_path / "local-key").read_text() == "oldvalue"


def test_existing_slot_is_archived_before_replacement(tmp_path):
    sink = Sink()
    (tmp_path / "local-key").write_text("oldvalue")
    (tmp_path / "local-key").chmod(0o640)
    slots(tmp_path, emit=sink.emit).store("local-key", "newvalue")
    assert (tmp_path / "local-key").read_text() == "newvalue"
    archived = list((tmp_path / "archive").iterdir())
    assert len(archived) == 1 and archived[0].read_text() == "oldvalue"
