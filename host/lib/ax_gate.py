"""S-6.13 SSH forced command and F-9.3 host-side seat provisioning.

The gate uses only the standard library. Host administration additionally needs
PyYAML (already an AX dependency). No host operation happens on import.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import io
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import select
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from typing import Callable

SEAT_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,31}\Z")
TIERS = {"read": 0, "operate": 1, "irreversible": 2}
GATE_PATH = "/usr/local/lib/ax-seat/ax"
AUTHORIZED_KEYS = Path("/var/lib/ax-seat/.ssh/authorized_keys")
DROPIN_PATH = Path("/etc/ssh/sshd_config.d/20-ax-seat.conf")
# This is the first-word allowlist from S-16.8. Full syntax remains in ax_cli.
READ_OPS = {"brief", "whoami", "guide", "status", "todos", "events", "show",
            "approvals", "seats", "decisions", "destinations", "results"}
OPERATE_OPS = {"work", "answer", "done", "park", "stop", "cancel", "resume",
               "control", "subloop", "change", "update", "decide", "handoff", "claim", "seat"}
FILE_FLAGS = {"--spec", "--go-file", "--build-go", "--out"}
GLOBAL_VALUES = {"--request-id", "--agent", "--model", "--session", "--format"}
SHELL_WORDS = re.compile(r"[;&|<>`]|\$[({]")


class GateError(Exception):
    def __init__(self, code="bad_request", exit_code=1):
        self.code = code
        self.exit_code = exit_code
        super().__init__(code)


def key_material(public_key: str) -> tuple[str, str]:
    """Reject options and multiline data; compute the OpenSSH SHA256 fingerprint."""
    if not isinstance(public_key, str) or any(c in public_key for c in "\x00\r\n"):
        raise GateError()
    words = public_key.split()
    if len(words) < 2 or words[0] not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}:
        raise GateError()
    try:
        raw = base64.b64decode(words[1], validate=True)
        size = struct.unpack(">I", raw[:4])[0]
        if not size or raw[4:4 + size].decode("ascii") != words[0] or len(raw) <= 4 + size:
            raise ValueError()
    except (ValueError, UnicodeError, struct.error):
        raise GateError() from None
    key = " ".join(words[:2])
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return key, fingerprint


def auth_fingerprint(auth_info: str) -> str:
    """ExposeAuthInfo records the successful publickey method and public key."""
    fingerprints = []
    for line in auth_info.splitlines():
        method, _, value = line.partition(" ")
        try:
            if method == "publickey":
                fingerprints.append(key_material(value)[1])
        except GateError:
            raise GateError("seat_unknown", 7) from None
    if len(fingerprints) != 1:
        raise GateError("seat_unknown", 7)
    return fingerprints[0]


def read_auth_info(environ: dict) -> str:
    path = environ.get("SSH_USER_AUTH")
    if not path:
        raise GateError("seat_unknown", 7)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise GateError("seat_unknown", 7)
        value = os.read(fd, 16385)
        if len(value) > 16384:
            raise GateError("seat_unknown", 7)
        return value.decode("utf-8")
    finally:
        os.close(fd)


def parse_command(command: str, tier: str) -> list[str]:
    if len(command.encode("utf-8")) > 8192 or any(c in command for c in "\x00\r\n"):
        raise GateError()
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        raise GateError() from None
    if any(re.fullmatch(r"[;&|<>]+", word) for word in words):
        raise GateError("operation_not_allowed", 6)
    if words[:1] == ["ax"]:
        words.pop(0)
    if not words:
        raise GateError()
    # Only declared metadata and output arguments may precede the operation.
    pos = 0
    while pos < len(words) and words[pos].startswith("--"):
        flag, eq, _ = words[pos].partition("=")
        if flag == "--json" and not eq:
            pos += 1
        elif flag in GLOBAL_VALUES:
            pos += 1 if eq else 2
        else:
            raise GateError("operation_not_allowed", 6)
    if pos >= len(words):
        raise GateError()
    op = words[pos]
    rest = words[pos + 1:]
    required = "read" if op in READ_OPS else "operate"
    if op in {"core", "permission", "go"}:
        required = "irreversible"
    elif op == "approval":
        if not rest or rest[0] not in {"show", "wait", "verify", "export-go", "withdraw"}:
            raise GateError("operation_not_allowed", 6)
        required = "operate" if rest[0] == "withdraw" else "read"
        if rest[0] == "export-go":
            # This operation always writes files, including its default output.
            raise GateError("operation_not_allowed", 6)
    elif op not in READ_OPS | OPERATE_OPS:
        raise GateError("operation_not_allowed", 6)
    if op == "seat" and rest != ["revoke"]:
        raise GateError("operation_not_allowed", 6)
    if op in {"core", "permission"} and rest[:1] != ["change"]:
        raise GateError("operation_not_allowed", 6)
    if op == "go" and rest[:1] not in (["ratify"], ["removal"]):
        raise GateError("operation_not_allowed", 6)
    if op == "go" and rest[:1] == ["ratify"] and rest[1:2] != ["-"]:
        raise GateError("operation_not_allowed", 6)
    for index, word in enumerate(words):
        flag, eq, value = word.partition("=")
        if flag in FILE_FLAGS:
            value = value if eq else (words[index + 1] if index + 1 < len(words) else "")
            if value != "-" or flag == "--out":
                raise GateError("operation_not_allowed", 6)
        if flag in {"--gate", "--socket", "--config", "--gate-url", "--gate-token", "--from", "--principal", "--issued-by", "--confirm-irreversible"}:
            raise GateError("forged_authority", 6)
    if tier not in TIERS or TIERS[tier] < TIERS[required]:
        raise GateError("tier_insufficient", 6)
    return words


def needs_stdin(words: list[str]) -> bool:
    return any(w in FILE_FLAGS and words[i + 1:i + 2] == ["-"] or w.startswith(tuple(f + "=" for f in FILE_FLAGS)) and w.endswith("=-") for i, w in enumerate(words)) or any(words[i:i + 3] == ["go", "ratify", "-"] for i in range(len(words)))


def bounded_stdin(stream, maximum: int) -> bytes:
    raw = stream.read(maximum + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > maximum:
        raise GateError()
    return raw


def check_payload(raw: bytes, seat_id: str) -> None:
    """Reject authority fields even in JSON submitted as a spec; never log it."""
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeError):
        return  # YAML is validated by the destination's schema in intake.
    if not isinstance(obj, dict):
        return
    if obj.get("from", "commander") != "commander" or obj.get("principal", seat_id) != seat_id or "issued_by" in obj or obj.get("confirm_irreversible", False) is not False:
        raise GateError("forged_authority", 6)


@dataclass(frozen=True)
class GateSettings:
    seat_socket: str
    gate_log: str
    seat_max_sessions: int = 4
    seat_idle_timeout_s: int = 900
    seat_stdin_max_kib: int = 1024

    @classmethod
    def load(cls, path: Path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise GateError("internal", 1)
        value = cls(**json.loads(path.read_text(encoding="utf-8")))
        if not Path(value.seat_socket).is_absolute() or not Path(value.gate_log).is_absolute():
            raise GateError("internal", 1)
        for v, lo, hi in [(value.seat_max_sessions, 1, 8), (value.seat_idle_timeout_s, 60, 3600), (value.seat_stdin_max_kib, 16, 8192)]:
            if type(v) is not int or not lo <= v <= hi:
                raise GateError("internal", 1)
        return value


class Gate:
    """backend owns authentication/session limits; dispatch is the normal CLI.

    Neither component is selected from the SSH environment or user arguments.
    Backend methods: open(seat_id, fingerprint, maximum), check(session),
    close(session, counters). open returns session, key_fp and tier.
    """
    def __init__(self, settings: GateSettings, backend, dispatch: Callable, *, audit=None):
        self.settings, self.backend, self.dispatch = settings, backend, dispatch
        self.audit = audit or self._audit

    def _audit(self, kind, seat_id, session=None, **data):
        # No command, input, public key, fingerprint, token or exception text.
        record = {"kind": kind, "seat_id": seat_id, "session": session,
                  "at": datetime.now(timezone.utc).isoformat(), **data}
        try:
            fd = os.open(self.settings.gate_log, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    return
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode())
            finally:
                os.close(fd)
        except OSError:
            pass  # Logging failure stays in this connection.

    def run(self, seat_id, command, auth_info, *, stdin, stdout, stderr, read_line=None):
        session = None
        counts = {"read": 0, "written": 0, "denied": 0}
        try:
            if not SEAT_ID.fullmatch(seat_id):
                raise GateError("seat_unknown", 7)
            fingerprint = auth_fingerprint(auth_info)
            opened = self.backend.open(seat_id, fingerprint, self.settings.seat_max_sessions)
            session = opened["session"]
            if not hmac.compare_digest(opened["key_fp"], fingerprint):
                raise GateError("seat_unknown", 7)
            self.audit("commander.session.opened", seat_id, session)
            tier = opened["tier"]

            def execute(text, input_stream):
                current = self.backend.check(session)
                active_tier = current.get("tier", tier)
                words = parse_command(text, active_tier)
                raw = bounded_stdin(input_stream, self.settings.seat_stdin_max_kib * 1024) if needs_stdin(words) else b""
                check_payload(raw, seat_id)
                # The dispatch receives an immutable, fixed-socket transport.
                code = self.dispatch(words, io.BytesIO(raw), stdout, stderr, session)
                if code == 0:
                    counts["read" if next((w for w in words if w in READ_OPS), None) else "written"] += 1
                else:
                    counts["denied"] += 1
                return code

            if command:
                return execute(command, stdin)
            execute("brief --short", io.BytesIO())
            stdout.write("AX の操作を1行ずつ入力する。\n終了するには標準入力を閉じる。\n使い方は ax guide で読む。\n")
            while True:
                if read_line:
                    line = read_line(self.settings.seat_idle_timeout_s)
                else:
                    ready, _, _ = select.select([stdin], [], [], self.settings.seat_idle_timeout_s)
                    if not ready:
                        break
                    line = stdin.readline(8194)
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                # One terminal line is the delimiter, not part of the command.
                line = line.removesuffix("\n").removesuffix("\r")
                if not line.strip():
                    continue
                try:
                    execute(line, io.BytesIO())
                except GateError as exc:
                    counts["denied"] += 1
                    self.audit("commander.denied", seat_id, session, reason=exc.code)
                    stderr.write(exc.code + "\n")
                    if exc.exit_code == 7:
                        return 7
            return 0
        except GateError as exc:
            counts["denied"] += 1
            self.audit("commander.denied", seat_id, session, reason=exc.code)
            stderr.write(exc.code + "\n")
            return exc.exit_code
        except (ConnectionError, FileNotFoundError, TimeoutError) as exc:
            self.audit("commander.denied", seat_id, session, reason="socket_unreachable")
            stderr.write("socket_unreachable\n")
            return 3
        except Exception:
            self.audit("commander.denied", seat_id, session, reason="internal")
            stderr.write("internal\n")
            return 1
        finally:
            if session is not None:
                try:
                    self.backend.close(session, counts)
                except Exception:
                    pass
                self.audit("commander.session.closed", seat_id, session, **counts)


def _active(seat, now):
    if seat.get("via") != "ssh" or seat.get("state") != "active":
        return False
    try:
        expiry = datetime.fromisoformat(seat["expires_at"].replace("Z", "+00:00"))
        return expiry.tzinfo is not None and expiry > now
    except (KeyError, ValueError, TypeError):
        return False


def authorized_lines(seats, peers, *, now=None, locked=False, gate_path=GATE_PATH):
    """Pure renderer; peers are the F-9.4 ip -> device identity mapping."""
    if locked:
        return [], set()
    if gate_path != GATE_PATH:
        if not re.fullmatch(r"/[a-zA-Z0-9_./-]+", gate_path):
            raise GateError()
    now = now or datetime.now(timezone.utc)
    lines, active, seen_keys, seen_ids = [], set(), set(), set()
    for seat in seats:
        if not _active(seat, now):
            continue
        seat_id = seat.get("seat_id", "")
        if not isinstance(seat_id, str) or not SEAT_ID.fullmatch(seat_id) or seat.get("tier") not in TIERS or seat_id in seen_ids:
            raise GateError()
        key, fp = key_material(seat.get("public_key"))
        if fp != seat.get("key_fp") or key in seen_keys:
            raise GateError()
        seen_keys.add(key)
        seen_ids.add(seat_id)
        addresses = []
        for ip, peer in peers.items():
            if not isinstance(peer, dict):
                raise GateError()
            if peer.get("device") != seat.get("device") or peer.get("device_id") != seat.get("device_id") or not seat.get("device_id"):
                continue
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                raise GateError() from None
            tailnet = addr in ipaddress.ip_network("100.64.0.0/10") if addr.version == 4 else addr in ipaddress.ip_network("fd7a:115c:a1e0::/48")
            if not tailnet:
                raise GateError()
            addresses.append(str(addr))
        if not addresses:
            continue
        options = f'restrict,pty,from="{",".join(sorted(addresses))}",command="{gate_path} --gate {seat_id}"'
        lines.append(f"{options} {key} ax-seat:{seat_id}\n")
        active.add(seat_id)
    return sorted(lines), active


def atomic_text(path: Path, text: str, mode: int):
    """Stage on HDD; replace with a same-directory staging inode if needed."""
    if path.is_symlink():
        raise GateError()
    temporary_dir = os.environ.get("TMPDIR", "/srv/hddtmp")
    fd, name = tempfile.mkstemp(prefix="ax-seat-", dir=temporary_dir)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # Fail without changing the old file when the HDD staging directory and
        # destination are on different filesystems (OPEN.md, b12b1-2).
        os.replace(name, path)
    except BaseException:
        # Preserve the HDD staging artifact for diagnosis; never delete it.
        raise


class HostOps:
    def __init__(self, profile, *, run=None, key_path=AUTHORIZED_KEYS, proc_root=Path("/proc"), kill=None):
        self.profile = profile
        self.run = run or self._run
        self.key_path, self.proc_root = Path(key_path), Path(proc_root)
        self.kill = kill
        self.user = profile.get("seat_user", "ax-seat")
        if self.user != "ax-seat":
            raise NotImplementedError("F-9.3 drop-in is fixed to ax-seat")
        self.state = Path(profile["ax_state_dir"])
        self.gate_path = profile.get("seat_gate_path", GATE_PATH)

    @staticmethod
    def _run(args):
        return subprocess.run(args, check=True, capture_output=True, text=True, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "TMPDIR": os.environ.get("TMPDIR", "/srv/hddtmp")}).stdout

    def stop_processes(self, active=None):
        uid = pwd.getpwnam(self.user).pw_uid
        stopped = []
        for entry in self.proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            pidfd = None
            try:
                # Pin identity before inspecting /proc, so PID reuse cannot
                # redirect a root signal to another user's process.
                if self.kill is None:
                    pidfd = os.pidfd_open(int(entry.name))
                if entry.stat().st_uid != uid:
                    continue
                argv = (entry / "cmdline").read_bytes().split(b"\0")
                args = [a.decode("utf-8") for a in argv if a]
                if active is not None:
                    if "--gate" not in args or self.gate_path not in args:
                        continue
                    index = args.index("--gate")
                    if index + 1 >= len(args) or args[index + 1] in active:
                        continue
                if self.kill is not None:
                    self.kill(int(entry.name), signal.SIGTERM)
                else:
                    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                stopped.append(int(entry.name))
            except (FileNotFoundError, ProcessLookupError, PermissionError, UnicodeError):
                continue
            finally:
                if pidfd is not None:
                    os.close(pidfd)
        return stopped

    def sync(self, seats, peers, *, locked=False, now=None):
        lines, active = authorized_lines(seats, peers, now=now, locked=locked, gate_path=self.gate_path)
        atomic_text(self.key_path, "".join(lines), 0o644)
        return {"lines": len(lines), "stopped": self.stop_processes(active)}

    def revoke_all(self):
        atomic_text(self.key_path, "", 0o644)
        return {"lines": 0, "stopped": self.stop_processes()}

    def status(self, seats):
        lines = self.key_path.read_text(encoding="utf-8").splitlines() if self.key_path.exists() else []
        return {"lines": len(lines), "seats": [{"seat_id": s["seat_id"], "state": s["state"]} for s in seats if s.get("via") == "ssh"]}

    def install(self, source: Path):
        """Called only at cutover, never by tests or artifact generation."""
        gate = Path(self.gate_path)
        required = [source / "host/ax", source / "host/lib/ax_cli/__init__.py", source / "host/lib/ax_gate.py"]
        if not all(p.is_file() for p in required):
            raise GateError("internal", 1)
        try:
            record = pwd.getpwnam(self.user)
        except KeyError:
            self.run(["groupadd", "--force", self.user])
            self.run(["useradd", "--gid", self.user, "--home-dir", "/var/lib/ax-seat", "--shell", "/bin/sh", "-p", "!", self.user])
        else:
            if record.pw_dir != "/var/lib/ax-seat" or record.pw_shell != "/bin/sh":
                raise GateError("internal", 1)
        # Existing accounts must never have sudo or additional groups.
        groups = self.run(["id", "-nG", self.user]).split()
        if set(groups) != {self.user}:
            raise GateError("internal", 1)
        for directory in [Path("/var/lib/ax-seat"), self.key_path.parent, gate.parent, gate.parent / "lib"]:
            self.run(["install", "-d", "-o", "root", "-g", "root", "-m", "0755", str(directory)])
        if not self.key_path.exists():
            atomic_text(self.key_path, "", 0o644)
        self.run(["chown", "root:root", str(self.key_path)])
        self.run(["chmod", "0644", str(self.key_path)])
        self.run(["install", "-o", "root", "-g", "root", "-m", "0755", str(source / "host/ax"), str(gate)])
        for name in ["ax_gate.py", "ax_cli", "ax_ops"]:
            src = source / "host/lib" / name
            if src.is_file():
                self.run(["install", "-o", "root", "-g", "root", "-m", "0644", str(src), str(gate.parent / "lib" / name)])
            elif src.is_dir():
                for file in src.rglob("*.py"):
                    target = gate.parent / "lib" / file.relative_to(source / "host/lib")
                    self.run(["install", "-d", "-o", "root", "-g", "root", "-m", "0755", str(target.parent)])
                    self.run(["install", "-o", "root", "-g", "root", "-m", "0644", str(file), str(target)])
        settings = {"seat_socket": self.profile.get("seat_socket", str(self.state / "seat/seat.sock")), "gate_log": str(self.state / "seat/gate.log")}
        atomic_text(gate.parent / "gate-settings.json", json.dumps(settings) + "\n", 0o644)
        # Do not reload sshd here. Stage the configuration for the separately
        # reviewed sshd -t / sshd -T comparison and reload procedure.
        self.run(["install", "-o", "root", "-g", "root", "-m", "0644", str(source / "host/sshd-dropin/20-ax-seat.conf"), str(gate.parent / "20-ax-seat.conf.pending")])
        return {"installed": str(gate), "sshd_pending": str(gate.parent / "20-ax-seat.conf.pending")}


def registry_snapshot(path: Path):
    """Read the {'seats': [...]} envelope published by intake/seat/store.py."""
    import yaml
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"seats"} or not isinstance(value["seats"], list):
        raise GateError()
    ids = set()
    for row in value["seats"]:
        if not isinstance(row, dict) or not isinstance(row.get("seat_id"), str) or not SEAT_ID.fullmatch(row["seat_id"]) or row["seat_id"] in ids:
            raise GateError()
        if row.get("via") not in {"ssh", "tailnet"} or row.get("state") not in {"active", "suspended", "revoked"} or row.get("tier") not in TIERS:
            raise GateError()
        ids.add(row["seat_id"])
    return value["seats"]


def ssh_lock_snapshot(profile):
    # Lock policy is held in intake's private SQLite, not in seats.yaml.
    # A host-side snapshot/transport for that policy is not specified.
    raise NotImplementedError("S-6.11/S-6.13: host seat-sync SSH lock-state delivery is unspecified")


def _append_host_operation(path: Path, record: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise GateError("internal", 1)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _send_host_notification(profile: dict, message: str) -> None:
    import urllib.request
    endpoint = profile.get("ntfy_endpoint")
    topic = profile.get("ntfy_topic")
    if not isinstance(endpoint, str) or not isinstance(topic, str) or not endpoint.startswith("http://") or not re.fullmatch(r"[A-Za-z0-9_.-]+", topic):
        raise GateError("internal", 1)
    request = urllib.request.Request(endpoint.rstrip("/") + "/" + topic, data=message.encode("utf-8"), method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        if not 200 <= response.status < 300:
            raise GateError("internal", 1)


def emergency_add(profile, pubkey_file, device, tier, *, append=None, notify=None, actor=None, now=None, seat_id=None):
    if tier != "read":
        raise GateError("tier_insufficient", 6)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,252}", device):
        raise GateError()
    key, fingerprint = key_material(Path(pubkey_file).read_text(encoding="utf-8").strip())
    now = now or datetime.now(timezone.utc)
    seat_id = seat_id or "emg-" + hashlib.sha256((fingerprint + now.isoformat()).encode()).hexdigest()[:20]
    if not SEAT_ID.fullmatch(seat_id):
        raise GateError()
    record = {"seat_id": seat_id, "via": "ssh", "tier": "read", "device": device,
              "public_key": key, "key_fp": fingerprint, "actor": actor or pwd.getpwuid(os.geteuid()).pw_name,
              "at": now.isoformat()}
    append = append or (lambda value: _append_host_operation(Path(profile["ax_state_dir"]) / "seat/host-ops.log", value))
    notify = notify or (lambda message: _send_host_notification(profile, message))
    append(record)
    try:
        notify("ホストから席が登録された")
    except Exception:
        # The durable log is the idempotent source for intake. Do not append a
        # second record on retry, and do not claim that the notification sent.
        raise GateError("internal", 1) from None
    return {"queued": True, "seat_id": seat_id, "tier": "read"}


def main(argv=None, *, gate=None, environ=None, stdin=None, stdout=None, stderr=None):
    """Public dispatch: ax_gate.main([seat_id]); --gate prefix also accepted."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--gate"]:
        argv.pop(0)
    environ = os.environ if environ is None else environ
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        if len(argv) != 1 or not SEAT_ID.fullmatch(argv[0]):
            raise GateError("seat_unknown", 7)
        if gate is None:
            # No state path is read from AX_SOCKET, AX_GATE_URL or config.yaml.
            settings = GateSettings.load(Path(__file__).resolve().parent.parent / "gate-settings.json")
            raise NotImplementedError("S-6.13: seat session wire and ax_cli gate-dispatch integration")
        return gate.run(argv[0], environ.get("SSH_ORIGINAL_COMMAND", ""), read_auth_info(environ), stdin=stdin, stdout=stdout, stderr=stderr)
    except GateError as exc:
        stderr.write(exc.code + "\n")
        return exc.exit_code
    except Exception:
        stderr.write("internal\n")
        return 1


def host_main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "sync", "revoke-all", "status", "add"])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--pubkey-file")
    parser.add_argument("--device")
    parser.add_argument("--tier", default="read")
    args = parser.parse_args(argv)
    try:
        import yaml
        profile = yaml.safe_load(Path(args.profile).read_text(encoding="utf-8"))
        # Resolve profile references locally; never use seat SSH environment.
        state = str(Path(profile["ax_state_dir"]).expanduser())
        for key, value in list(profile.items()):
            if isinstance(value, str):
                profile[key] = value.replace("${ax_state_dir}", state)
        profile["ax_state_dir"] = state
        ops = HostOps(profile)
        if args.action in {"install", "sync", "revoke-all"} and os.geteuid() != 0:
            raise GateError("operation_not_allowed", 6)
        if args.action == "install":
            result = ops.install(Path(__file__).resolve().parents[2])
        elif args.action == "revoke-all":
            result = ops.revoke_all()
        elif args.action == "add":
            if not args.pubkey_file or not args.device:
                raise GateError()
            result = emergency_add(profile, args.pubkey_file, args.device, args.tier)
        else:
            seats = registry_snapshot(Path(profile.get("seats_file", str(ops.state / "seat/seats.yaml"))))
            if args.action == "status":
                result = ops.status(seats)
            else:
                locked = ssh_lock_snapshot(profile)
                peers = json.loads((ops.state / "gate/peers.json").read_text(encoding="utf-8"))
                result = ops.sync(seats, peers, locked=locked)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except GateError as exc:
        print(exc.code, file=sys.stderr)
        return exc.exit_code
    except NotImplementedError:
        print("internal: 未確定の接続契約がある。build/OPEN.md を確認する。", file=sys.stderr)
        return 1
    except Exception:
        print("internal", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(host_main())
