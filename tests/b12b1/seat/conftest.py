from __future__ import annotations

import base64
from pathlib import Path
import struct

import pytest

from axcommon import timeutil
from axcommon.defaults import Defaults
from core.intake.approval import VerifiedUser
from core.intake.seat.service import SeatService
from core.intake.seat.store import SeatStore, digest, key_fingerprint
from tests.fakes.b12b1.seat.parts import FakeEffects


@pytest.fixture
def row_factory(fake_clock):
    def make(seat_id="seat-one", via="ssh", **updates):
        row = dict(seat_id=seat_id, label="test", via=via, device="device-one", device_id="stable-one",
                   tier="operate", state="active", created_at=timeutil.now_ts(), created_by="approval-test",
                   expires_at=timeutil.add_seconds(timeutil.now_ts(), 86400))
        if via == "ssh":
            alg = b"ssh-ed25519"
            key = seat_id.encode().ljust(32, b"0")[:32]
            raw = struct.pack(">I", len(alg)) + alg + struct.pack(">I", len(key)) + key
            public_key = "ssh-ed25519 " + base64.b64encode(raw).decode()
            row.update(public_key=public_key, key_fp=key_fingerprint(public_key))
        else:
            row["token_digest"] = digest("test-token-" + seat_id)
        row.update(updates)
        return row
    return make


@pytest.fixture
def seat_env(tmp_path, fake_clock):
    alerts, events = [], []
    store = SeatStore(tmp_path / "seat" / "seats.yaml", tmp_path / "intake.sqlite",
                      archive_dir=tmp_path / "archive" / "seat", alert=alerts.append)
    store.initialize()
    effects = FakeEffects()
    service = SeatService(store, seat_uid=lambda: 1234, gate_address="fake-gate",
                          gate_token=lambda: "fake-core-token", defaults=Defaults.load(),
                          effects=effects.mapping(), emit=lambda *args: events.append(args),
                          hq_verifier=lambda scope, body: VerifiedUser(body.get("proof") == "fake-user-proof", body.get("proof") == "fake-user-proof", True))
    yield service, store, effects, events, alerts
    store.close()
