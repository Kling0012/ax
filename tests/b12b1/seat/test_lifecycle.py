from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os

import pytest

from axcommon import timeutil
from core.intake.seat.service import RelayEvidence
from core.intake.seat.store import SeatError, SeatStore, digest
from core.intake.vias import Ident

HQ = Ident("hq", "hq", "user", "irreversible")
LOCAL = Ident("local", "commander", "local-example", "operate")


def test_t191_revoke_only_requester_and_retry_failed_effect(seat_env, row_factory):
    service, store, effects, _, _ = seat_env
    store.put(row_factory())
    store.put(row_factory("seat-two"))
    effects.fail.add("release_claims")
    service.revoke("seat-one", Ident("ssh", "commander", "seat-one", "operate"))
    assert store.get("seat-one")["state"] == "revoked"
    assert store.get("seat-two")["state"] == "active"
    assert effects.pending["seat-one"] == [] and effects.pending["seat-two"] == ["approval-b"]
    assert effects.controls["seat-one"] == [] and effects.controls["seat-two"] == ["command-b"]
    assert store.pending_effects()[0]["action"] == "release_claims"
    effects.fail.clear()
    service.drain_effects()
    assert not store.pending_effects() and effects.claims["seat-two"] == ["claim-b"]
    with pytest.raises(SeatError, match="seat_revoked"):
        service.ssh(service.ssh_transport_ident(1234), "seat-one")


def test_t191_other_seat_revocation_rejected(seat_env, row_factory):
    service, store, *_ = seat_env
    store.put(row_factory())
    with pytest.raises(SeatError, match="forged_authority"):
        service.revoke("seat-one", Ident("ssh", "commander", "seat-two", "irreversible"))
    assert store.get("seat-one")["state"] == "active"


def test_t191_expired_seat_refused_once_and_others_continue(seat_env, row_factory, fake_clock):
    service, store, effects, events, _ = seat_env
    store.put(row_factory(expires_at=timeutil.add_seconds(timeutil.now_ts(), 1)))
    store.put(row_factory("seat-two"))
    fake_clock.advance(1)
    for _ in range(2):
        with pytest.raises(SeatError, match="seat_expired"):
            service.ssh(service.ssh_transport_ident(1234), "seat-one")
    assert sum(e[0] == "seat.expired" for e in events) == 1
    assert service.ssh(service.ssh_transport_ident(1234), "seat-two").principal == "seat-two"


def test_t191_lock_and_lockdown_do_not_affect_hq(seat_env, row_factory):
    service, store, *_ = seat_env
    store.put(row_factory())
    service.set_lock(HQ, "tailnet", True)
    assert service.ssh(service.ssh_transport_ident(1234), "seat-one").max_tier == "operate"
    assert service.local_tier(LOCAL).max_tier == "operate"
    service.lockdown(HQ, True)
    assert service.local_tier(LOCAL).max_tier == "read"
    assert service.local_tier(HQ) == HQ
    with pytest.raises(SeatError, match="locked"):
        service.ssh(service.ssh_transport_ident(1234), "seat-one", read=True)
    with pytest.raises(SeatError, match="forged_authority"):
        service.lockdown(LOCAL, False, approved=True)
    service.lockdown(HQ, False, approved=True)
    assert service.ssh(service.ssh_transport_ident(1234), "seat-one").max_tier == "operate"


def test_t191_suspension_has_read_only_exception_for_auto_suspension(seat_env, row_factory):
    service, store, effects, events, _ = seat_env
    store.put(row_factory())
    ident = service.ssh(service.ssh_transport_ident(1234), "seat-one")
    for _ in range(int(service.defaults.get("commander_reject_suspend"))):
        service.count_result(ident, True)
    with pytest.raises(SeatError, match="seat_suspended"):
        service.ssh(service.ssh_transport_ident(1234), "seat-one")
    assert service.ssh(service.ssh_transport_ident(1234), "seat-one", read=True).max_tier == "read"
    assert any(e[0] == "seat.suspended" for e in events)
    assert any(call[0] == "notify_suspended" for call in effects.calls)


def test_t191_rate_suspend_is_per_seat(seat_env, row_factory):
    service, store, *_ = seat_env
    store.put(row_factory())
    store.put(row_factory("seat-two"))
    ident = service.ssh(service.ssh_transport_ident(1234), "seat-one")
    with pytest.raises(SeatError, match="seat_suspended"):
        for _ in range(int(service.defaults.get("commander_rate_max")) + 1):
            service.count_instruction(ident)
    assert service.ssh(service.ssh_transport_ident(1234), "seat-two").max_tier == "operate"


def test_t196_code_once_digest_only_and_token_bound_to_device(seat_env):
    service, store, _, events, _ = seat_env
    code = service.start_enrollment({"label": "remote", "tier": "read"}, "approval-test", bound_device_id="stable-one")
    evidence = RelayEvidence("fake-core-token", "stable-one", "device-one")
    with pytest.raises(SeatError):
        service.enroll(code, RelayEvidence("fake-core-token", "stable-two", "device-two"))
    output = service.enroll(code, evidence)
    token = output["token"]
    ident = service.relay("fake-gate", RelayEvidence("fake-core-token", "stable-one", "device-one", token))
    assert ident.principal == output["seat_id"]
    with pytest.raises(SeatError, match="peer_unknown"):
        service.relay("fake-gate", RelayEvidence("fake-core-token", "stable-two", "device-two", token))
    with pytest.raises(SeatError):
        service.enroll(code, evidence)
    row = store.get(output["seat_id"])
    assert row["token_digest"] == digest(token)
    assert code not in store.path.read_text() and token not in store.path.read_text()
    stored = list(store.db.execute("SELECT * FROM enroll_codes"))
    assert stored[0]["code_digest"] == digest(code)
    for secret in (code, token):
        assert secret not in str(events)
        assert secret.encode() not in store.db.execute("SELECT content FROM seat_versions ORDER BY version DESC LIMIT 1").fetchone()[0]
    service.revoke(ident.principal, HQ)
    with pytest.raises(SeatError, match="seat_revoked"):
        service.relay("fake-gate", RelayEvidence("fake-core-token", "stable-one", "device-one", token))


def test_t196_expiry_and_source_lock_persist_across_service_restart(seat_env, fake_clock):
    service, store, *_ = seat_env
    code = service.start_enrollment({"label": "remote", "tier": "read"}, "approval-test", bound_device_id="stable-one")
    fake_clock.advance(float(service.defaults.get("gate_enroll_code_min")) * 60)
    evidence = RelayEvidence("fake-core-token", "stable-one", "device-one")
    for _ in range(int(service.defaults.get("gate_enroll_fail_max"))):
        with pytest.raises(SeatError, match="unauthorized"):
            service.enroll(code, evidence)
    with pytest.raises(SeatError, match="locked"):
        service.enroll(code, evidence)
    # 不正コードの停止は別の端末へ波及しない。
    another = service.start_enrollment({"label": "remote", "tier": "read"}, "approval-test", bound_device_id="stable-two")
    assert service.enroll(another, RelayEvidence("fake-core-token", "stable-two", "device-two"))["seat_id"]
    fake_clock.advance(float(service.defaults.get("gate_enroll_lock_min")) * 60)
    with pytest.raises(SeatError, match="unauthorized"):
        service.enroll(code, evidence)


def test_t196_concurrent_exchange_issues_one_token(seat_env):
    service, store, *_ = seat_env
    code = service.start_enrollment({"label": "remote", "tier": "read"}, "approval-test", bound_device_id="stable-one")
    evidence = RelayEvidence("fake-core-token", "stable-one", "device-one")

    def exchange(_):
        try:
            return service.enroll(code, evidence)
        except SeatError:
            return None

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(exchange, range(3)))
    assert sum(result is not None for result in results) == 1
    assert len(store.rows) == 1


def test_t196_unbound_pre_exchange_binding_enrolls_first_verified_device_and_refuses_other_device(seat_env):
    service, store, *_ = seat_env
    code = service.start_enrollment({"label": "remote", "tier": "read"}, "approval-test")
    evidence = RelayEvidence("fake-core-token", "stable-one", "device-one")

    enrolled = service.enroll(code, evidence)
    token = enrolled["token"]
    assert store.get(enrolled["seat_id"])["device_id"] == "stable-one"
    assert service.relay("fake-gate", RelayEvidence("fake-core-token", "stable-one", "device-one", token)).principal == enrolled["seat_id"]
    with pytest.raises(SeatError, match="peer_unknown"):
        service.relay("fake-gate", RelayEvidence("fake-core-token", "stable-two", "device-two", token))
    with pytest.raises(SeatError, match="unauthorized"):
        service.enroll(code, evidence)


def test_t209_tamper_uses_intake_written_version_and_survives_restart(seat_env, row_factory, tmp_path):
    service, store, _, _, alerts = seat_env
    row = row_factory()
    store.put(row)
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    store.path.write_text("seats: [not-a-seat]")
    assert store.get("seat-one") == row
    assert store.degraded and "seats_integrity_mismatch" in alerts
    assert service.ssh(service.ssh_transport_ident(1234), "seat-one", read=True).principal == "seat-one"
    with pytest.raises(SeatError, match="unavailable"):
        service.check_available(Ident("ssh", "commander", "seat-one"), {"destination": "work.request"})
    service.check_available(Ident("ssh", "commander", "seat-one"), {"destination": "work.stop"})
    restarted = SeatStore(store.path, tmp_path / "intake.sqlite", archive_dir=store.archive, alert=alerts.append)
    try:
        assert restarted.get("seat-one") == row and restarted.degraded
    finally:
        restarted.close()
    assert list(store.archive.glob("*.yaml"))


def test_t209_valid_yaml_with_raised_tier_is_not_adopted(seat_env, row_factory):
    service, store, *_ = seat_env
    store.put(row_factory(tier="read"))
    store.path.write_text(store.path.read_text().replace("tier: read", "tier: irreversible"))
    assert service.ssh(service.ssh_transport_ident(1234), "seat-one", read=True).max_tier == "read"


def test_t209_first_unreadable_ledger_keeps_local_and_hq_working(tmp_path, seat_env):
    service, _, *_ = seat_env
    path = tmp_path / "broken.yaml"
    path.write_text("seats: [broken]")
    store = SeatStore(path, tmp_path / "other.sqlite", archive_dir=tmp_path / "archive")
    original, service.store = service.store, store
    try:
        with pytest.raises(SeatError, match="seat_unknown"):
            service.ssh(service.ssh_transport_ident(1234), "seat-one")
        assert service.local_tier(LOCAL).max_tier == "operate"
        assert service.local_tier(HQ) == HQ
        with pytest.raises(SeatError, match="unavailable"):
            store.initialize()
    finally:
        service.store = original
        store.close()


def test_t209_corrupt_database_does_not_crash_component(tmp_path):
    dbpath = tmp_path / "broken.sqlite"
    dbpath.write_bytes(b"not a sqlite database")
    alerts = []
    store = SeatStore(tmp_path / "seats.yaml", dbpath, archive_dir=tmp_path / "archive", alert=alerts.append)
    try:
        assert not store.database_ok and "seat_store_unavailable" in alerts
        with pytest.raises(SeatError, match="unavailable"):
            store.get("seat-one")
    finally:
        store.close()
