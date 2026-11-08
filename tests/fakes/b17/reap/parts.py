"""Only in-memory commands/notes and local filesystem transfers; no subprocesses."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from subprocess import CompletedProcess

from axcommon import ids, timeutil
from core.archiver.rsync import TransferError
from tools.cutover.reap import Ports


class FakeOperator:
    def __init__(self, paths):
        self.paths = paths
        self.claim = True
        self.active = False
        self.enabled = False
        self.restart_ok = True
        self.notes_ok = True
        self.commands = []
        self.notes = []
        self.restarts = 0
        self.after_restart = None
        self.last_ms = 0

    def authorize(self):
        return self.claim

    def run(self, argv):
        self.commands.append(argv)
        assert argv[:2] == ["systemctl", "--user"]
        assert argv[3] in {"archive-to-hdd.service", "archive-to-hdd.timer"}
        if argv[2] == "is-active":
            return CompletedProcess(argv, 0 if self.active else 3, "active\n" if self.active else "inactive\n")
        assert argv[2] == "is-enabled"
        return CompletedProcess(argv, 0 if self.enabled else 1, "enabled\n" if self.enabled else "disabled\n")

    def restart(self):
        import yaml
        self.restarts += 1
        assert not self.paths.marker.exists()
        doc = yaml.safe_load(self.paths.sources_file.read_text())
        sources = doc["sources"] if isinstance(doc, dict) else doc
        assert all(s["remove_original"] is (s["name"] != "claude-projects") for s in sources)
        if self.after_restart:
            self.after_restart()
        return self.restart_ok

    def note(self, kind, subject, target, result, reason):
        if not self.claim or not self.notes_ok:
            return 8 if not self.claim else 3
        self.last_ms += 1
        event = {
            "id": ids.new_ulid(), "ts": timeutil.format_ts(timeutil.now() + __import__("datetime").timedelta(milliseconds=self.last_ms)),
            "source": {"component": "cutover-operator", "host": self.paths.host_id,
                       "principal": "operator-seat", "session": "reap-test", "via": "ssh"},
            "kind": kind, "subject": subject, "project": None, "subloop": None,
            "data": {"target": target, "result": result, "reason": reason,
                     "external_item_id": None, "destination": None, "delivery_state": None, "active_sender": None},
            "cause": ids.new_ulid(),
        }
        self.paths.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.journal.open("a") as stream:
            stream.write(json.dumps(event) + "\n")
        self.notes.append(event)
        return 0

    def ports(self):
        return Ports(self.authorize, self.run, self.note, self.restart)


class LocalTransfer:
    """A local copy followed by content digest verification of the copied bytes."""
    def __init__(self, destination: Path):
        self.destination = destination
        self.corrupt = False
        self.down = False
        self.unverifiable = False
        self.after_verify = None
        self.calls = []

    def ensure_dirs(self, relative):
        self.calls.append("ensure_dirs")
        if self.down:
            raise TransferError("connection_failed")
        (self.destination / relative).mkdir(parents=True, exist_ok=True)

    def send(self, root, relatives, destination):
        self.calls.append("send")
        for rel in relatives:
            target = self.destination / destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, target)
            if self.corrupt:
                data = target.read_bytes()
                target.write_bytes(bytes([data[0] ^ 1]) + data[1:])

    def verify(self, root, relatives, destination):
        self.calls.append("verify")
        if self.unverifiable:
            raise TransferError("verify_failed")
        for rel in relatives:
            local = (root / rel).read_bytes()
            remote = (self.destination / destination / rel).read_bytes()
            if hashlib.sha256(local).digest() != hashlib.sha256(remote).digest():
                assert len(local) == len(remote)
                raise TransferError("verify_mismatch")
        if self.after_verify:
            self.after_verify()

    def put_file(self, local, destination):
        self.calls.append("put_file")
        shutil.copy2(local, self.destination / destination / local.name)
