"""T-186/T-187/T-191/T-209: SSH host artifacts with fake host operations."""

import ipaddress as _fixture_ipaddress
_NET_FIXTURE_3010 = str(_fixture_ipaddress.ip_network("100.64.0.0/10")[3010])
import base64
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import stat
import struct
import threading
from types import SimpleNamespace

import pytest

import ax_gate as gate
from seat_backend import FakeDispatch, FakeSeatBackend


def public_key(byte=1):
    alg = b"ssh-ed25519"
    raw = struct.pack(">I", len(alg)) + alg + struct.pack(">I", 32) + bytes([byte]) * 32
    return "ssh-ed25519 " + base64.b64encode(raw).decode()


def row(seat_id="seat-one", byte=1):
    key = public_key(byte)
    return {"seat_id": seat_id, "via": "ssh", "state": "active", "tier": "operate",
            "device": "fake-device", "device_id": "fake-stable-id", "public_key": key,
            "key_fp": gate.key_material(key)[1], "expires_at": "2099-01-01T00:00:00Z"}


@pytest.fixture
def harness(tmp_path):
    rows = [row(), row("seat-two", 2), row("seat-three", 3)]
    backend = FakeSeatBackend(rows)
    dispatch = FakeDispatch()
    audit = []
    settings = gate.GateSettings(str(tmp_path / "seat.sock"), str(tmp_path / "gate.log"))
    runner = gate.Gate(settings, backend, dispatch, audit=lambda *a, **k: audit.append((a, k)))
    return SimpleNamespace(rows=rows, backend=backend, dispatch=dispatch, audit=audit, runner=runner)


def invoke(harness, command="ax status", *, seat_id="seat-one", data=b"", read_line=None, key=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    auth = "publickey " + (key or harness.backend.rows[seat_id]["public_key"])
    result = harness.runner.run(seat_id, command, auth, stdin=io.BytesIO(data), stdout=stdout,
                                stderr=stderr, read_line=read_line)
    return result, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize("command", ["id", "ls", "sh", "scp -t x", "sftp", "rsync --server x", "git status",
                                            "ax status; id", "ax status && id", "ax status | sh", "ax status > x",
                                            "ax --socket /bad status", "ax --gate seat-two", "ax seat lock ssh"])
def test_t186_external_commands_and_redirection_fail(harness, command):
    assert invoke(harness, command)[0] in {1, 6}
    assert harness.dispatch.calls == []
    assert harness.backend.sessions == {}


@pytest.mark.parametrize("command", ["ax status\nid", "ax status\r", "ax status\x00", "ax " + "x" * 8192, "ax 'status"])
def test_t186_command_size_and_control_bytes(harness, command):
    assert invoke(harness, command)[0] == 1
    assert harness.dispatch.calls == []


@pytest.mark.parametrize("command", ["ax core change --spec /etc/passwd", "ax core change --spec=/etc/passwd",
                                            "ax approval verify id --go-file /etc/passwd", "ax go ratify /etc/passwd",
                                            "ax approval export-go id", "ax permission change --spec ../../other"])
def test_t186_file_paths_are_not_read(harness, command):
    harness.backend.rows["seat-one"]["tier"] = "irreversible"
    assert invoke(harness, command)[0] == 6
    assert harness.dispatch.calls == []


def test_t186_stdin_is_bounded_and_tier_checked(harness):
    harness.backend.rows["seat-one"]["tier"] = "irreversible"
    command = "ax core change --spec -"
    assert invoke(harness, command, data=b"{}") [0] == 0
    assert harness.dispatch.calls[-1][1] == b"{}"
    harness.dispatch.calls.clear()
    assert invoke(harness, command, data=b"x" * (1024 * 1024 + 1))[0] == 1
    assert harness.dispatch.calls == []
    harness.backend.rows["seat-one"]["tier"] = "read"
    assert invoke(harness, "ax work --project fake --title task")[0] == 6


def test_t186_literal_punctuation_in_a_title_is_data(harness):
    assert invoke(harness, 'ax work --project fake --title "a;b $(id)"')[0] == 0
    assert harness.dispatch.calls[-1][0][-1] == "a;b $(id)"


def test_t186_interactive_brief_and_no_command_brief(harness):
    lines = iter(["ax status\n", ""])
    result, stdout, _ = invoke(harness, "", read_line=lambda timeout: next(lines))
    assert result == 0
    assert harness.dispatch.calls[0][0] == ["brief", "--short"]
    assert harness.dispatch.calls[1][0] == ["status"]
    assert len(stdout.splitlines()) <= 25
    harness.dispatch.calls.clear()
    assert invoke(harness)[0] == 0
    assert [c[0] for c in harness.dispatch.calls] == [["status"]]


def test_t186_session_limit_is_per_seat(harness):
    key_fp = harness.rows[0]["key_fp"]
    opened = [harness.backend.open("seat-one", key_fp, 4) for _ in range(4)]
    assert invoke(harness)[0] == 7
    assert invoke(harness, seat_id="seat-two")[0] == 0
    harness.backend.close(opened[0]["session"], {})
    assert invoke(harness)[0] == 0


def test_t186_idle_timeout_uses_default(harness):
    timeouts = []
    assert invoke(harness, "", read_line=lambda seconds: timeouts.append(seconds) or "")[0] == 0
    assert timeouts == [900]


def test_t186_dropin_applies_to_seat_only(repo_root):
    text = (repo_root / "host/sshd-dropin/20-ax-seat.conf").read_text()
    assert text.startswith("#")
    directives = [line.strip().split() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert directives[0] == ["Match", "User", "ax-seat"]
    assert directives[-1] == ["Match", "all"]
    values = dict((line[0], " ".join(line[1:])) for line in directives[1:-1])
    for name in ["AllowTcpForwarding", "AllowStreamLocalForwarding", "AllowAgentForwarding", "X11Forwarding", "PermitTunnel", "PermitUserRC", "PasswordAuthentication", "PermitUserEnvironment"]:
        assert values[name] == "no"
    assert values["MaxSessions"] == "4"
    assert values["ExposeAuthInfo"] == "yes"
    assert values["AuthorizedKeysFile"] == "/var/lib/ax-seat/.ssh/authorized_keys"
    assert "ListenAddress" not in text and "Port " not in text


def test_t187_expose_auth_info_mismatch_is_denied(harness):
    assert invoke(harness, key=public_key(99))[0] == 7
    assert harness.dispatch.calls == []


def test_t187_expose_auth_info_ambiguous_or_bad_is_denied():
    for auth in ["", "password", "publickey not-a-key", "publickey " + public_key() + "\npublickey " + public_key(2)]:
        with pytest.raises(gate.GateError) as error:
            gate.auth_fingerprint(auth)
        assert error.value.exit_code == 7


@pytest.mark.parametrize("field,value", [("from", "hq"), ("principal", "user"), ("issued_by", "user"), ("confirm_irreversible", True)])
def test_t187_authority_spoofing_in_stdin_is_rejected(harness, field, value):
    harness.backend.rows["seat-one"]["tier"] = "irreversible"
    assert invoke(harness, "ax core change --spec -", data=json.dumps({field: value}).encode())[0] == 6
    assert harness.dispatch.calls == []


def test_t187_declared_model_cannot_raise_tier(harness):
    harness.backend.rows["seat-one"]["tier"] = "read"
    assert invoke(harness, "ax --agent user --model powerful work --project fake --title test")[0] == 6


def test_t187_cli_env_cannot_choose_connection(harness, monkeypatch, tmp_path):
    auth = tmp_path / "auth-info"
    auth.write_text("publickey " + public_key())
    auth.chmod(0o600)
    seen = []
    original = harness.dispatch
    def dispatch(*args):
        seen.append(harness.runner.settings.seat_socket)
        return original(*args)
    harness.runner.dispatch = dispatch
    env = {"SSH_USER_AUTH": str(auth), "SSH_ORIGINAL_COMMAND": "ax status", "AX_SOCKET": "/attacker.sock",
           "AX_GATE_URL": "http://invalid.example", "HOME": str(tmp_path)}
    code = gate.main(["seat-one"], gate=harness.runner, environ=env, stdin=io.BytesIO(), stdout=io.StringIO(), stderr=io.StringIO())
    assert code == 0 and seen == [harness.runner.settings.seat_socket]
    assert "/attacker.sock" not in seen


def test_t191_authorized_keys_exact_shape_and_source_binding():
    seats = [row(), row("revoked-seat", 2), row("expired-seat", 3), row("other-device", 4)]
    seats[1]["state"] = "revoked"
    seats[2]["expires_at"] = "2000-01-01T00:00:00Z"
    seats[3]["device_id"] = "different-stable-id"
    peers = {"__NET_FIXTURE_3010__".replace('__NET_FIXTURE_3010__', _NET_FIXTURE_3010): {"device": "fake-device", "device_id": "fake-stable-id"}}
    lines, active = gate.authorized_lines(seats, peers)
    assert active == {"seat-one"}
    assert lines == [f'restrict,pty,from="{_NET_FIXTURE_3010}",command="/usr/local/lib/ax-seat/ax --gate seat-one" {public_key()} ax-seat:seat-one\n']
    assert gate.authorized_lines(seats, peers, locked=True) == ([], set())


@pytest.mark.parametrize("field,value", [("seat_id", 'bad\";id'), ("tier", "root"), ("public_key", 'command="sh" ' + public_key()), ("key_fp", "wrong")])
def test_t191_registry_injection_is_rejected(field, value):
    seat = row()
    seat[field] = value
    with pytest.raises(gate.GateError):
        gate.authorized_lines([seat], {})


def test_t191_peer_address_and_identity_must_match():
    assert gate.authorized_lines([row()], {"__NET_FIXTURE_3010__".replace('__NET_FIXTURE_3010__', _NET_FIXTURE_3010): {"device": "fake-device", "device_id": "different"}}) == ([], set())
    with pytest.raises(gate.GateError):
        gate.authorized_lines([row()], {"1.2.3.4": {"device": "fake-device", "device_id": "fake-stable-id"}})


def test_t191_sync_and_revoke_all_use_fake_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(gate.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1234))
    procs = tmp_path / "proc"
    procs.mkdir()
    for pid, args in [(101, ["python3", gate.GATE_PATH, "--gate", "seat-one"]),
                      (102, ["python3", gate.GATE_PATH, "--gate", "revoked-seat"]),
                      (103, ["some-other-program"])]:
        folder = procs / str(pid)
        folder.mkdir()
        (folder / "cmdline").write_bytes(b"\x00".join(a.encode() for a in args))
    path_stat = Path.stat
    def fake_stat(path, *args, **kwargs):
        result = path_stat(path, *args, **kwargs)
        if path.parent == procs:
            return SimpleNamespace(st_uid=1234)
        return result
    monkeypatch.setattr(Path, "stat", fake_stat)
    killed = []
    target = tmp_path / "authorized_keys"
    ops = gate.HostOps({"ax_state_dir": str(tmp_path)}, key_path=target, proc_root=procs, kill=lambda pid, sig: killed.append(pid))
    peers = {"__NET_FIXTURE_3010__".replace('__NET_FIXTURE_3010__', _NET_FIXTURE_3010): {"device": "fake-device", "device_id": "fake-stable-id"}}
    assert ops.sync([row()], peers)["lines"] == 1
    assert killed == [102]
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert "seat-one" in target.read_text()
    killed.clear()
    assert ops.revoke_all()["lines"] == 0
    assert set(killed) == {101, 102, 103}
    assert target.read_text() == ""


def test_t191_invalid_sync_does_not_touch_keys_or_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    target = tmp_path / "authorized_keys"
    target.write_text("old\n")
    ops = gate.HostOps({"ax_state_dir": str(tmp_path)}, key_path=target)
    monkeypatch.setattr(ops, "stop_processes", lambda *args: pytest.fail("must not stop processes"))
    seat = row()
    seat["seat_id"] = "bad/seat"
    with pytest.raises(gate.GateError):
        ops.sync([seat], {})
    assert target.read_text() == "old\n"


def test_t191_revocation_is_checked_on_every_interactive_command(harness):
    lines = iter(["ax status\n", "ax status\n", ""])
    def revoke_after_status():
        if harness.dispatch.calls[-1][0] == ["status"]:
            harness.backend.rows["seat-one"]["state"] = "revoked"
    harness.dispatch.hook = revoke_after_status
    assert invoke(harness, "", read_line=lambda _: next(lines))[0] == 7
    assert len(harness.dispatch.calls) == 2
    assert invoke(harness, seat_id="seat-two")[0] == 0


def test_t191_emergency_add_does_not_write_registry(tmp_path):
    keyfile = tmp_path / "public.pub"
    keyfile.write_text(public_key())
    registry = tmp_path / "seats.yaml"
    registry.write_text("seats: []\n")
    records, notices = [], []
    profile = {"ax_state_dir": str(tmp_path)}
    with pytest.raises(gate.GateError):
        gate.emergency_add(profile, keyfile, "fake-device", "operate", append=records.append, notify=notices.append)
    assert records == notices == []
    queued = gate.emergency_add(profile, keyfile, "fake-device", "read", append=records.append, notify=notices.append,
                                seat_id="emg-test")
    assert queued == {"queued": True, "seat_id": "emg-test", "tier": "read"}
    assert records[0]["tier"] == "read" and records[0]["seat_id"] == "emg-test" and notices
    assert registry.read_text() == "seats: []\n"


def test_t209_gate_exception_is_local_and_logs_no_input(harness):
    harness.dispatch.failure = RuntimeError("secret-do-not-log")
    assert invoke(harness)[0] == 1
    assert harness.backend.sessions == {}
    assert "secret-do-not-log" not in repr(harness.audit)
    harness.dispatch.failure = None
    assert invoke(harness, seat_id="seat-two")[0] == 0
    assert harness.backend.closed


def test_t209_intake_down_returns_three(harness):
    harness.backend.failure = ConnectionRefusedError("private detail")
    assert invoke(harness)[0] == 3
    assert harness.dispatch.calls == []
    assert "private detail" not in repr(harness.audit)


def test_t209_three_seats_answer_without_global_wait(harness):
    barrier = threading.Barrier(3, timeout=3)
    harness.dispatch.hook = lambda: barrier.wait()
    results = []
    threads = [threading.Thread(target=lambda s=s: results.append(invoke(harness, seat_id=s)[0])) for s in harness.backend.rows]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not any(t.is_alive() for t in threads)
    assert sorted(results) == [0, 0, 0]
    assert len(harness.backend.closed) == 3


def test_t209_real_gate_wire_stays_explicitly_unimplemented(monkeypatch, tmp_path):
    # Never make an unauthenticated fallback to commander.sock while the session
    # protocol is undefined in S-6.13 and intake/seat/middleware.py.
    monkeypatch.setattr(gate.GateSettings, "load", classmethod(lambda cls, path: gate.GateSettings(str(tmp_path / "seat.sock"), str(tmp_path / "gate.log"))))
    stderr = io.StringIO()
    assert gate.main(["seat-one"], environ={"AX_SOCKET": "/wrong"}, stderr=stderr) == 1
    assert stderr.getvalue() == "internal\n"
