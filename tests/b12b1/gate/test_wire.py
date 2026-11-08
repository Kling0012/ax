from __future__ import annotations

import asyncio
import json

import pytest

from core.gate.contracts import RelayEvidence, RelayRequest
from core.gate.relay import HttpRelay
from core.intake.seat.service import decode_relay_evidence
from core.intake.seat.store import SeatError


def test_gate_relay_and_intake_decoder_share_credential_headers(monkeypatch):
    async def exchange():
        response = json.dumps({"ok": True}).encode()
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(response)).encode() + b"\r\n\r\n" + response)
        reader.feed_eof()

        class IntakeWriter:
            def __init__(self):
                self.data = bytearray()
                self.closed = False

            def write(self, data):
                self.data.extend(data)

            async def drain(self):
                pass

            def close(self):
                self.closed = True

            async def wait_closed(self):
                pass

        writer = IntakeWriter()

        async def open_connection(host, port):
            assert (host, port) == ("intake", 8082)
            return reader, writer

        monkeypatch.setattr("core.gate.relay.asyncio.open_connection", open_connection)
        request = RelayRequest(
            "POST", "/v1/instructions",
            RelayEvidence("core-token", "stable-device", "device.tailnet.test", "seat-token"),
            {"agent": "test-agent", "model": "test-model", "session": "test-session"},
            {"destination": "work.request"}, "01JTESTREQUEST",
        )
        reply = await HttpRelay()(request, 1)
        return bytes(writer.data), writer.closed, reply

    wire, closed, reply = asyncio.run(exchange())
    head, marker, body = wire.partition(b"\r\n\r\n")
    request_line, *header_lines = head.decode("ascii").split("\r\n")
    headers = {name.lower(): value for name, value in (line.split(": ", 1) for line in header_lines)}
    assert reply.status == 200 and reply.data == {"ok": True}
    assert closed and marker == b"\r\n\r\n"
    assert request_line == "POST /v1/instructions HTTP/1.1"
    assert json.loads(body) == {"destination": "work.request"}
    assert headers == {
        "host": "intake:8082",
        "accept": "application/json",
        "content-type": "application/json; charset=utf-8",
        "content-length": "30",
        "x-ax-gate-token": "core-token",
        "x-ax-device-id": "stable-device",
        "x-ax-device": "device.tailnet.test",
        "x-ax-request-id": "01JTESTREQUEST",
        "authorization": "Bearer seat-token",
        "x-ax-agent": "test-agent",
        "x-ax-model": "test-model",
        "x-ax-session": "test-session",
    }
    evidence = decode_relay_evidence({"scope": {"path": "/v1/instructions", "headers": [(name.encode(), value.encode()) for name, value in headers.items()]}})
    assert (evidence.core_token, evidence.device_id, evidence.device, evidence.bearer) == (
        "core-token", "stable-device", "device.tailnet.test", "seat-token",
    )


def test_relay_decoder_rejects_duplicate_or_missing_authority_headers():
    scope = {"headers": [
        (b"x-ax-gate-token", b"core-token"),
        (b"x-ax-gate-token", b"other-token"),
        (b"x-ax-device-id", b"stable-device"),
        (b"x-ax-device", b"device.tailnet.test"),
        (b"x-ax-request-id", b"01JTESTREQUEST"),
    ]}
    with pytest.raises(SeatError, match="unauthorized"):
        decode_relay_evidence({"scope": scope})


@pytest.mark.parametrize("authorization", [
    pytest.param([], id="missing"),
    pytest.param([(b"authorization", b"")], id="empty"),
    pytest.param([(b"authorization", b"Bearer one"), (b"authorization", b"Bearer two")], id="duplicate"),
])
def test_non_enroll_relay_decoder_requires_one_nonempty_authorization_header(authorization):
    headers = [
        (b"x-ax-gate-token", b"core-token"),
        (b"x-ax-device-id", b"stable-device"),
        (b"x-ax-device", b"device.tailnet.test"),
        (b"x-ax-request-id", b"01JTESTREQUEST"),
        *authorization,
    ]
    with pytest.raises(SeatError, match="unauthorized"):
        decode_relay_evidence({"scope": {"path": "/v1/read/status", "headers": headers}})
