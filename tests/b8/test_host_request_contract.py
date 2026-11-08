"""要求ファイルの形（core/observer/hostreq.py が書き、host/lib/hostwatch/requests.py が読む）と schemas/ext/b8/host-request.schema.json の一致。"""
import json
import os
import stat
from pathlib import Path

import pytest

from axcommon import ids
from core.observer import hostreq

ROOT = Path(__file__).resolve().parents[2]


def schema_errors(obj):
    import jsonschema

    s = json.loads((ROOT / "schemas" / "ext" / "b8" / "host-request.schema.json").read_text())
    return [e.message for e in jsonschema.Draft202012Validator(s).iter_errors(obj)]


def test_written_requests_validate_against_the_schema(tmp_path):
    for op, name in (("restart", "ledger"), ("deploy", None), ("seat-sync", None)):
        p = hostreq.write_request(tmp_path, op, name)
        assert schema_errors(json.loads(p.read_text())) == []
        assert p.name.endswith(".req") and ids.is_ulid(p.name[:-4])
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
        assert hostreq.parse_body(p.read_bytes()) == (op, name)


@pytest.mark.parametrize("body", [
    {"op": "restart"}, {"op": "restart", "name": "X"}, {"op": "deploy", "name": "a"}, {"op": "restart", "name": "a", "image": "x"},
    {"op": "build"}, {"name": "a"}, [], "x",
    {"op": "deploy", "name": None}, {"op": "seat-sync", "name": None},
    {"op": "restart", "name": "ledger\n"},
])
def test_bad_bodies_fail_both_schema_and_parser(body):
    assert schema_errors(body) != []
    with pytest.raises(ValueError):
        hostreq.parse_body(json.dumps(body).encode())


def test_write_refuses_bad_arguments_and_leaves_no_temp_files(tmp_path):
    for args in (("restart", None), ("restart", "A b"), ("deploy", "x"), ("nope", None)):
        with pytest.raises(ValueError):
            hostreq.write_request(tmp_path, *args)
    assert list(tmp_path.iterdir()) == []


def test_temp_name_starts_with_dot_so_readers_ignore_it(tmp_path):
    hostreq.write_request(tmp_path, "deploy")
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []
    assert len(hostreq.pending(tmp_path)) == 1
