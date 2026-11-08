import errno
import os
import shutil
from pathlib import Path

import pytest

from core.archiver.config import Dest, Source
from core.archiver.engine import Archiver
from core.archiver.ledger import ArchiverLedger
from core.archiver.rsync import Transport
from tests.fakes.b1.fake_remote import FakeRemote

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync unavailable")


class Events:
    def __init__(self):
        self.events = []

    def emit(self, kind, subject, data=None, **kwargs):
        self.events.append((kind, subject, data))
        return {}


def setup_archiver(tmp_path, monkeypatch):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    index = tmp_path / "index"
    index.write_text("test-salt\n")
    pii = tmp_path / "pii"
    pii.write_text("Private Person\n")
    monkeypatch.setenv("AX_REDACT_INDEX", str(index))
    monkeypatch.setenv("AX_PII_LIST", str(pii))
    root = tmp_path / "source"
    root.mkdir()
    src = Source(name="source", path=str(root), root=root, min_age_days=0)
    remote = FakeRemote(tmp_path / "remote")
    events = Events()
    archiver = Archiver([src], Transport(Dest("example.com", 22, "user"), remote.runner),
                        ArchiverLedger(":memory:"), events, state_dir=tmp_path / "state")
    return archiver, src, remote, events


def test_secret_candidates_are_held_and_clean_file_is_transferred(tmp_path, monkeypatch, caplog):
    archiver, src, remote, events = setup_archiver(tmp_path, monkeypatch)
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmno"
    (src.root / "jwt").write_text(jwt)
    (src.root / "pii").write_text("Private Person")
    (src.root / "mail").write_text("Co-Authored-By: someone@example.com")
    (src.root / "binary").write_bytes(b"\xff")
    (src.root / "good").write_text("ordinary content")
    result = archiver.run_once().sources[0]
    transferred = list(remote.root.rglob("good"))
    assert len(transferred) == 1 and transferred[0].read_text() == "ordinary content"
    assert result.transferred == 1 and result.failed == "inspection_held"
    assert not list(remote.root.rglob("jwt")) and not list(remote.root.rglob("pii"))
    assert not list(remote.root.rglob("mail")) and not list(remote.root.rglob("binary"))
    assert jwt not in caplog.text and jwt not in repr(events.events)
    assert (archiver.stage_root.stat().st_mode & 0o777) == 0o700
    assert list(archiver.stage_root.rglob("jwt"))


def test_source_change_after_inspection_sends_only_staged_content(tmp_path, monkeypatch):
    archiver, src, remote, _ = setup_archiver(tmp_path, monkeypatch)
    original = src.root / "good"
    original.write_text("safe content")
    runner = remote.runner
    def change_before_send(argv, **kwargs):
        if any(x.startswith("--files-from=") for x in argv) and "--dry-run" not in argv:
            original.write_text("Private Person")
        return runner(argv, **kwargs)
    archiver.transport.runner = change_before_send
    report = archiver.run_once().sources[0]
    assert list(remote.root.rglob("good"))[0].read_text() == "safe content"
    assert original.read_text() == "Private Person" and report.changed_after == 1


@pytest.mark.parametrize("missing_env", ["AX_PII_LIST", "AX_REDACT_INDEX"])
def test_missing_inspection_configuration_holds_source(tmp_path, monkeypatch, missing_env):
    archiver, src, remote, _ = setup_archiver(tmp_path, monkeypatch)
    (src.root / "good").write_text("ordinary content")
    monkeypatch.setenv(missing_env, str(tmp_path / "absent"))
    assert archiver.run_once().sources[0].failed == "inspection_unavailable"
    assert not list(remote.root.rglob("good"))


def test_mtime_is_preserved_without_copying_public_permissions(tmp_path, monkeypatch):
    archiver, src, remote, _ = setup_archiver(tmp_path, monkeypatch)
    original = src.root / "good"
    original.write_text("ordinary content")
    original.chmod(0o644)
    timestamp = 1_600_000_000_123_456_789
    os.utime(original, ns=(timestamp, timestamp))
    result = archiver.run_once().sources[0]
    copied = next(archiver.stage_root.rglob("good"))
    received = next(remote.root.rglob("good"))
    assert result.transferred == 1
    assert copied.stat().st_mtime_ns == timestamp
    assert received.stat().st_mtime_ns == timestamp
    assert copied.stat().st_mode & 0o777 == 0o600
    assert received.stat().st_mode & 0o777 == 0o600
    assert original.stat().st_mode & 0o777 == 0o644


def test_indexed_secret_is_held_and_clean_file_continues(tmp_path, monkeypatch, caplog):
    from axcommon.redact_index import value_hash

    archiver, src, remote, events = setup_archiver(tmp_path, monkeypatch)
    secret = "indigo42marble73"
    (tmp_path / "index").write_text("test-salt\n" + value_hash("test-salt", secret) + "\n")
    (src.root / "secret").write_text(secret)
    (src.root / "good").write_text("ordinary content")
    result = archiver.run_once().sources[0]
    assert result.transferred == 1 and result.failed == "inspection_held"
    assert not list(remote.root.rglob("secret"))
    assert next(remote.root.rglob("good")).read_text() == "ordinary content"
    assert secret not in caplog.text and secret not in repr(events.events)


def test_staging_full_holds_only_affected_candidate(tmp_path, monkeypatch):
    archiver, src, remote, _ = setup_archiver(tmp_path, monkeypatch)
    (src.root / "a-full").write_text("ordinary content")
    (src.root / "b-good").write_text("ordinary content")
    copy = shutil.copyfileobj

    def fail_one(source, target, *args, **kwargs):
        if Path(target.name).name == "a-full":
            raise OSError(errno.ENOSPC, "No space left on device")
        return copy(source, target, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfileobj", fail_one)
    result = archiver.run_once().sources[0]
    assert result.transferred == 1 and result.failed == "inspection_held"
    assert not list(remote.root.rglob("a-full"))
    assert next(remote.root.rglob("b-good")).read_text() == "ordinary content"
    assert (src.root / "a-full").exists() and list(archiver.stage_root.rglob("a-full"))


def test_staging_directory_is_excluded_from_reenumeration(tmp_path, monkeypatch):
    archiver, src, remote, _ = setup_archiver(tmp_path, monkeypatch)
    stage_tmp = src.root / "staging"
    stage_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(stage_tmp))
    archiver.stage_root = stage_tmp / "ax-archiver"
    (src.root / "good").write_text("ordinary content")
    assert archiver.run_once().sources[0].transferred == 1
    assert list(archiver.stage_root.rglob("good"))
    (src.root / "new").write_text("another ordinary file")
    assert archiver.candidates(src) == ["new"]
    assert archiver.run_once().sources[0].transferred == 1
    assert next(remote.root.rglob("new")).read_text() == "another ordinary file"
    assert not list(remote.root.rglob("ax-archiver"))
