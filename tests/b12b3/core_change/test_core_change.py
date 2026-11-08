import io
import json
from pathlib import Path

import pytest
import yaml

from host.lib.ax_ops.core_change import prepare
from core.intake.dest.b12 import core_change

ROOT = Path(__file__).resolve().parents[3]


def test_spec_file_and_stdin(tmp_path):
    payload = {"name": "change-one", "project": "ax", "title": "Change", "body": "Details",
               "acceptance": {"examples": ["works"], "boundaries": ["no bypass"]}}
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert prepare(str(path)) == ("core.change", payload, "ax")
    assert prepare("-", io.StringIO(json.dumps(payload))) == ("core.change", payload, "ax")


@pytest.mark.parametrize("contents", ["{", "[]", '"text"', "null"])
def test_bad_spec_rejected(contents):
    with pytest.raises(ValueError):
        prepare("-", io.StringIO(contents))


def test_missing_spec_rejected(tmp_path):
    with pytest.raises(ValueError):
        prepare(str(tmp_path / "missing"))


def test_registry_preserves_approval_policy():
    row = yaml.safe_load((ROOT / "config/intake-destinations.d/b12b3-core-change.yaml").read_text())[0]
    assert row == {"name": "core.change", "title": "core.change", "schema": "schemas/dest/core.change.json",
                   "handler": "b12:core.change", "allowed_from": ["commander", "hq"],
                   "via": ["local", "ssh", "tailnet", "hq"], "tier": "irreversible",
                   "irreversible": True, "ack": "sync", "default_expiry_min": 60,
                   "mcp_tool": "ax_core_change"}


def test_direct_handler_refuses_forged_approval():
    status, result = core_change.handle(None, {}, {"issued_by": "user", "approved_via": "fake",
                                                   "confirm_irreversible": True}, None)
    assert status == 403
    assert result["error"]["code"] == "approval_required"
