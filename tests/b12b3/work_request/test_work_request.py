import argparse
from io import StringIO
import json
from pathlib import Path
import sys

import pytest

from axcommon import schemas
from core.intake.registry import build_registry, discover_files, parse_files, unresolved_contract
from host.lib.ax_ops.work_request import build_payload, configure_parser
from core.intake.dest.b12 import work
from tests.fakes.b12.work_core.peers import setup

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "host/lib"))
import ax_cli


def test_registry_overrides_only_handler_and_registers_existing_work():
    config = ROOT / "config"
    docs, faults = parse_files(discover_files(config))
    entries, registry_faults = build_registry(docs)
    assert not faults and not registry_faults
    previous = [entry for entry in docs if entry["name"] == "work.request"][-2]
    effective = entries["work.request"]
    assert {**previous, "handler": "b12:work.request"} == {k: v for k, v in effective.items() if not k.startswith("_")}
    assert unresolved_contract(effective) is None
    app, _, _, _ = setup()
    work.register(app)
    assert app.state.intake.dest_handlers["work.request"] is work.handle


def test_cli_implementation_payload_reaches_existing_handler():
    parser = argparse.ArgumentParser()
    configure_parser(parser)
    args = parser.parse_args(["--project", "ax", "--title", "task", "--body", "implement",
                              "--example", "works", "--example", "retries", "--boundary", "no deploy",
                              "--verify", "check", "--role", "impl_hard"])
    payload = build_payload(args)
    assert not schemas.errors("dest/work.request.json", payload)
    app, peers, _, _ = setup()
    status, result = work.handle(app, {}, {"payload": payload, "idempotency_key": "abc"}, None)
    assert status == 200 and result["todo_id"]
    assert len(peers.items) == 1
    item = next(iter(peers.items.values()))
    assert item["acceptance"] == {"examples": ["works", "retries"], "boundaries": ["no deploy"]}
    assert item["role"] == "impl_hard"


@pytest.mark.parametrize("argv", [
    ["--example", "works"],
    ["--verify", "run"],
])
def test_read_role_cannot_send_write_fields(argv):
    calls = []
    def transport(method, path, body):
        calls.append((method, path, body))
        return 200, {}
    out = StringIO()
    code = ax_cli.main(["work", "--project", "ax", "--title", "task", "--role", "research",
                        "--body", "inspect", "--target", "repo", "--revision", "a" * 40, *argv],
                       transport=transport, environ={}, stdout=out, stderr=StringIO())
    assert code != 0 and json.loads(out.getvalue())["error"]["code"] == "bad_request"
    assert not calls
