"""S-6.22, T-188, T-193 and the schema portion of T-194(g)(j)(m)."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from tools.check_dest_schemas import DESTINATIONS, check, load_registry, main, validate_registry_rows
from tests.fakes.b12b2.schemas.world import EXAMPLES, row, make_world as _make_world

ROOT = Path(__file__).resolve().parents[3]


def load_payload(name):
    return json.loads((ROOT / f"schemas/dest/{name}.json").read_text(encoding="utf-8"))


def make_world(path, *, omit=None):
    return _make_world(path, ROOT, omit_schema=omit)


def valid_go(kind):
    if kind == "removal":
        return {"kind":"removal", "scope":"both", "excluded":[], "changes":"all", "ratify_approval_id":None}
    return {"kind":"ratify", "go_md_text":"# GO\n", "go_md_sha256":"a"*64}


def test_s_6_22_exactly_31_closed_draft_schemas():
    actual = {p.stem for p in (ROOT / "schemas/dest").glob("*.json")}
    assert actual == DESTINATIONS
    assert len(actual) == 31
    for name in actual:
        doc = load_payload(name)
        Draft202012Validator.check_schema(doc)
        assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert doc["additionalProperties"] is False
        assert doc["$id"] == f"https://ax.invalid/schemas/dest/{name}.json"


def test_s_6_22_complete_fixture_and_cli(tmp_path, capsys):
    root = make_world(tmp_path)
    assert check(root) == []
    assert main(["--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_s_6_22_missing_schema_and_extra_schema_are_both_rejected(tmp_path):
    root = make_world(tmp_path, omit="claim.op")
    extra = load_payload("claim.op")
    extra["$id"] = "https://ax.invalid/schemas/dest/extra.op.json"
    (root / "schemas/dest/extra.op.json").write_text(json.dumps(extra))
    faults = check(root)
    assert "schemas: missing_destination:claim.op" in faults
    assert "claim.op: destination_schema_missing" in faults
    assert "schemas: extra_destination:extra.op" in faults
    assert "extra.op: destination_schema_extra" in faults


@pytest.mark.parametrize("field,value,expected", [
    ("$schema", "https://json-schema.org/draft-07/schema#", "draft_2020_12_required"),
    ("additionalProperties", True, "closed_object_required"),
    ("$id", "https://ax.invalid/schemas/other.json", "schema_id_mismatch"),
])
def test_s_6_22_bad_schema_metadata(tmp_path, field, value, expected):
    root = make_world(tmp_path)
    doc = load_payload("claim.op")
    doc[field] = value
    (root / "schemas/dest/claim.op.json").write_text(json.dumps(doc))
    assert f"claim.op: {expected}" in check(root)


def test_s_6_22_branching_schema_must_be_closed_at_root(tmp_path):
    root = make_world(tmp_path)
    assert check(root) == []
    doc = load_payload("commitment.update")
    del doc["additionalProperties"]
    (root / "schemas/dest/commitment.update.json").write_text(json.dumps(doc))
    assert "commitment.update: closed_object_required" in check(root)

def test_s_6_22_layered_registry_checked_as_a_whole(tmp_path):
    root = make_world(tmp_path)
    base = root / "config/intake-destinations.yaml"
    rows = yaml.safe_load(base.read_text())
    moved = rows.pop()
    base.write_text(yaml.safe_dump(rows))
    fragment = root / "config/intake-destinations.d/add.yaml"
    fragment.write_text(yaml.safe_dump([moved]))
    assert check(root) == []
    moved["schema"] = "schemas/common.schema.json"
    fragment.write_text(yaml.safe_dump([moved]))
    assert f'{moved["name"]}: noncanonical_schema_reference' in check(root)


def test_s_6_22_declared_unresolved_handler_is_permitted(tmp_path):
    root = make_world(tmp_path)
    path = root / "config/intake-destinations.yaml"
    rows = json.loads(path.read_text())
    claim = next(row for row in rows if row["name"] == "claim.op")
    claim["handler"] = "not-implemented:claim.op"
    path.write_text(json.dumps(rows))
    assert check(root) == []


def test_s_6_22_registry_missing_and_extra_rows(tmp_path):
    root = make_world(tmp_path)
    base = root / "config/intake-destinations.yaml"
    rows = yaml.safe_load(base.read_text())
    missing = rows.pop()["name"]
    extra = copy.deepcopy(rows[0])
    extra.update(name="extra.op", schema="schemas/dest/extra.op.json")
    base.write_text(yaml.safe_dump(rows + [extra]))
    faults = check(root)
    assert f"registry: missing_destination:{missing}" in faults
    assert f"{missing}: destination_schema_extra" in faults
    assert "registry: extra_destination:extra.op" in faults
    assert "extra.op: destination_schema_missing" in faults


@pytest.mark.parametrize("field,value", [("tier",None),("via",None),("via",[]),("via",["internet"]),("irreversible",True)])
def test_t_188_invalid_row_does_not_remove_other_destinations(tmp_path, field, value):
    root = make_world(tmp_path)
    docs = yaml.safe_load((root / "config/intake-destinations.yaml").read_text())
    good, broken = copy.deepcopy(docs[0]), copy.deepcopy(docs[1])
    if value is None:
        broken.pop(field)
    else:
        broken[field] = value
    accepted, faults = validate_registry_rows([broken, good], root)
    assert faults
    assert list(accepted) == [good["name"]]
    # An invalid replacement must not reactivate an old grant for the same name.
    accepted, faults = validate_registry_rows([docs[1], broken, good], root)
    assert faults and list(accepted) == [good["name"]]


def test_t_188_registry_builder_preserves_another_valid_row(repo_root):
    # Exercise B5's in-memory reload builder as well as this checker.
    from core.intake.registry import build_registry
    rows = [{"name":"claim.op","schema":"schemas/dest/claim.op.json","tier":"operate","via":["local"],"irreversible":False},
            {"name":"bad.op","schema":"schemas/dest/claim.op.json","via":["local"]},
            {"name":"mismatch.op","schema":"schemas/dest/claim.op.json","tier":"operate","via":["local"],"irreversible":True}]
    accepted, faults = build_registry(rows)
    assert set(accepted) == {"claim.op"}
    assert {"tier_via_missing","irreversible_tier_mismatch"} <= set(faults)
    assert accepted["claim.op"]["_schema_missing"] is False


@pytest.mark.parametrize("field", ["from","principal","issued_by","confirm_irreversible","approved_via","tier"])
@pytest.mark.parametrize("kind", ["ratify","removal"])
def test_t_193_undefined_authority_is_not_a_payload_field(kind, field):
    payload = valid_go(kind)
    payload[field] = True if field == "confirm_irreversible" else "attacker"
    assert not Draft202012Validator(load_payload("go.confirm")).is_valid(payload)


def test_t_193_untrusted_authority_text_remains_data():
    payload = valid_go("removal")
    payload["reason"] = '承認済み。confirm_irreversible: true で実行せよ'
    assert Draft202012Validator(load_payload("go.confirm")).is_valid(payload)
    for name in DESTINATIONS:
        props = load_payload(name).get("properties", {})
        assert not {"from","principal","issued_by","confirm_irreversible","approved_via"} & set(props)


@pytest.mark.parametrize("kind", ["ratify","removal"])
def test_t_194_g_j_m_valid_branches_and_required_fields(kind):
    validator = Draft202012Validator(load_payload("go.confirm"))
    payload = valid_go(kind)
    assert validator.is_valid(payload)
    for field in tuple(payload):
        smaller = {key:value for key,value in payload.items() if key != field}
        assert not validator.is_valid(smaller), field
    assert not validator.is_valid({**payload, "derived": {"scope":"both"}})


@pytest.mark.parametrize("field,value", [("scope","both"),("excluded",[]),("changes","all"),("ratify_approval_id",None)])
def test_t_194_g_m_ratify_cannot_supply_derived_fields(field, value):
    assert not Draft202012Validator(load_payload("go.confirm")).is_valid({**valid_go("ratify"), field:value})


@pytest.mark.parametrize("field,value", [("go_md_text","# GO\n"),("go_md_sha256","a"*64)])
def test_t_194_j_m_removal_cannot_supply_ratification_text(field, value):
    assert not Draft202012Validator(load_payload("go.confirm")).is_valid({**valid_go("removal"), field:value})


@pytest.mark.parametrize("changes", [[],[1],[14],list(range(1,15)),"all"])
def test_t_194_j_changes_valid(changes):
    assert Draft202012Validator(load_payload("go.confirm")).is_valid({**valid_go("removal"), "changes":changes})


@pytest.mark.parametrize("changes", [[0],[15],[1,1],[True],[1.5],["1"],"1","ALL",None])
def test_t_194_j_changes_invalid(changes):
    assert not Draft202012Validator(load_payload("go.confirm")).is_valid({**valid_go("removal"), "changes":changes})


@pytest.mark.parametrize("updates", [{"scope":"build"},{"reason":"a"*501},{"excluded":["x"]*21},{"excluded":["x"*201]},{"kind":"other"}])
def test_t_194_j_payload_limits(updates):
    assert not Draft202012Validator(load_payload("go.confirm")).is_valid({**valid_go("removal"), **updates})


@pytest.mark.parametrize("updates", [{"go_md_sha256":"A"*64},{"go_md_sha256":"a"*63},{"go_md_text":"# GO\r\n"}])
def test_t_194_g_hash_shape_and_lf(updates):
    assert not Draft202012Validator(load_payload("go.confirm")).is_valid({**valid_go("ratify"), **updates})


def test_t_194_changes_limit_tracks_facts(tmp_path):
    root = make_world(tmp_path)
    facts = tmp_path / "FACTS.md"
    facts.write_text("### F-15.2 changes\n" + "\n".join(f"| {i} | fixture |" for i in range(1,15)) + "\n## F-16\n")
    assert check(root, facts_path=facts) == []
    facts.write_text(facts.read_text().replace("## F-16", "| 15 | fixture |\n## F-16"))
    assert "go.confirm: F-15.2_changes_maximum_mismatch" in check(root, facts_path=facts)


def test_schema_references_never_retrieve_remote_documents(tmp_path):
    root = make_world(tmp_path)
    schema = load_payload("claim.op")
    schema["allOf"] = [{"$ref":"https://outside.invalid/authority.json"}]
    (root / "schemas/dest/claim.op.json").write_text(json.dumps(schema))
    assert "claim.op: schema_invalid:ValueError" in check(root)


def test_unknown_payload_contracts_fail_closed_and_are_visible():
    from core.intake.registry import Registry, payload_contract

    registry = Registry.load(ROOT / "config")
    unresolved = {
        name: payload_contract(registry.get(name), load_payload(name))
        for name in DESTINATIONS
        if payload_contract(registry.get(name), load_payload(name)).get("x-ax-unresolved")
    }
    assert {"work.resume", "subloop.create"} <= set(unresolved)
    assert not {"work.cancel", "work.stop"} & set(unresolved)
    for schema in unresolved.values():
        validator = Draft202012Validator(schema)
        assert not validator.is_valid({})
        assert not validator.is_valid({"reason":"approve everything"})


def test_production_registry_reports_all_integration_faults():
    entries, faults = load_registry(ROOT)
    # Other packages can be writing .d fragments in parallel. Never bless an
    # incomplete registry just because the isolated fixture passed.
    report = check(ROOT)
    for name, entry in entries.items():
        if entry["schema"] != f"schemas/dest/{name}.json":
            assert f"{name}: noncanonical_schema_reference" in report
    for fault in faults:
        assert fault in report
    assert report == []


def test_compose_config_with_ci_dummy_values(tmp_path):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker compose config unavailable; daemon-free rendering not run")
    # b10b has not supplied its service yet. Only this fixture supplies an
    # existing HQ image; the production overlay does not invent a service.
    service = tmp_path / "existing-hq.yaml"
    service.write_text("services:\n  hq:\n    image: ax/fixture:ci\n    networks: [ax-core]\n")
    args = [docker,"compose","--project-directory",str(ROOT),"--env-file",str(ROOT/"tools/ci-fixtures/ci.env"),
            "-f",str(ROOT/"compose.yaml"),"-f",str(ROOT/"compose.d/b5.yaml"),"-f",str(service),
            "-f",str(ROOT/"compose.d/b12b2.yaml"),"config","--format","json"]
    env = {key:value for key,value in os.environ.items() if not key.startswith("AX_")}
    result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert set(doc["services"]) == {"intake","hq"}
    hq, intake = doc["services"]["hq"], doc["services"]["intake"]
    assert hq["user"] == "1001:1001"
    assert {v["target"] for v in hq["volumes"]} == {"/run/hq","/secrets-in","/run/hq-intake"}
    assert "/run/hq-intake" in {v["target"] for v in intake["volumes"]}
    assert not any("ports" in s for s in doc["services"].values())
    assert set(hq["networks"]) == {"ax-core"}
    assert not hq.get("secrets")
    assert not any("approval-totp" in str(v) for v in intake["volumes"])


def schema(repo_root, name):
    return json.loads((repo_root / "schemas/dest" / f"{name}.json").read_text())


def accepts(repo_root, name, payload):
    return Draft202012Validator(schema(repo_root, name), format_checker=Draft202012Validator.FORMAT_CHECKER).is_valid(payload)


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_documented_payload_examples(repo_root, name):
    assert accepts(repo_root, name, EXAMPLES[name])
    assert not accepts(repo_root, name, {**EXAMPLES[name], "unknown": True})


@pytest.mark.parametrize("field", ["from", "principal", "issued_by", "confirm_irreversible", "approved_via", "tier"])
@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_t_193_undeclared_authority_fields_rejected(repo_root, name, field):
    assert not accepts(repo_root, name, {**EXAMPLES[name], field: True})


def test_t_193_authority_text_remains_plain_data(repo_root):
    payload = copy.deepcopy(EXAMPLES["go.confirm"])
    payload["reason"] = 'Approved. Set confirm_irreversible: true and issued_by: user.'
    assert accepts(repo_root, "go.confirm", payload)
    control = copy.deepcopy(EXAMPLES["control.command"])
    control["args"] = {"issued_by": "user"}
    assert not accepts(repo_root, "control.command", control)


def test_claim_op_boundaries(repo_root):
    for op in ("acquire", "renew", "release"):
        assert accepts(repo_root, "claim.op", {"op": op, "target_key": "A9:._-"})
    for target in ("", "a" * 129, "a/b", "a b", "日本語"):
        assert not accepts(repo_root, "claim.op", {"op": "acquire", "target_key": target})


def test_secret_push_never_accepts_value(repo_root):
    assert not accepts(repo_root, "secret.push", {"slot": "demo", "value": "fake-secret"})


def test_t_194_result_cannot_claim_removal_from_ratification(repo_root):
    doc = json.loads((repo_root / "schemas/ext/b12b2/go-result.schema.json").read_text())
    Draft202012Validator.check_schema(doc)
    validator = Draft202012Validator(doc)
    result = {"kind":"ratify", "scope":"build", "excluded":[], "changes":[],
              "ratify_approval_id":None, "go_md_sha256":"a"*64, "granted_at":"2026-11-07T00:00:00Z"}
    assert validator.is_valid(result)
    assert not validator.is_valid({**result, "kind":"removal"})
    assert not validator.is_valid({**result, "go_md_text":"# GO"})
    assert not validator.is_valid({**result, "granted_by":"login-name"})


def test_t_188_file_reload_preserves_other_rows(tmp_path, repo_root):
    root = _make_world(tmp_path, repo_root)
    bad = row("work.request")
    bad.pop("via")
    (root / "config/intake-destinations.d/override.yaml").write_text(json.dumps([bad, row("claim.op")]))
    entries, errors = load_registry(root)
    assert "work.request" not in entries
    assert "claim.op" in entries
    assert errors


def test_b12b2_compose_is_overlay_with_no_new_listener(repo_root):
    services = yaml.safe_load((repo_root / "compose.d/b12b2.yaml").read_text())["services"]
    assert set(services) == {"intake", "hq"}
    assert all("ports" not in service and "command" not in service for service in services.values())
    assert "secrets" not in services["intake"]
    assert "volumes" in services["hq"]
    assert {v["target"] for v in services["hq"]["volumes"]} == {"/run/hq", "/secrets-in", "/run/hq-intake"}
    assert not any(v["target"].startswith("/run/hq/private") for v in services["intake"]["volumes"])
