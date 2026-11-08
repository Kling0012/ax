from copy import deepcopy
import json

import pytest

from plugins.subloop.change.verification import EvidenceCache, complete, load_recipes, run_verification
from tests.fakes.b14.verification.runner import FakeRunner

BASE = "a" * 40
HEAD = "b" * 40


def request(**updates):
    req = {"repo": "example/project", "base_sha": BASE, "head_sha": HEAD, "test_recipe": "test",
           "untrusted": True, "files_complete": True,
           "changed_files": [{"path": "src/code.py", "status": "modified", "binary": False}],
           "outbound_allowlist": ["registry.example"]}
    req.update(updates)
    return req


def recipes():
    return {"test": {"version": "1", "runner": "verify-python", "timeout_s": 60,
                     "dependency": ["fetch-deps"], "checks": [{"kind": "all", "command": "run-all"}],
                     "all": {"kind": "all", "command": "run-all"}}}


def run(req=None, runner=None, **kw):
    return run_verification(req or request(), runner or FakeRunner(), scrub=deepcopy, recipes=recipes(), **kw)


@pytest.mark.parametrize("bad", [
    {"files_complete": False}, {"changed_files": None},
    {"changed_files": [{"path": "src/code.py", "status": "added"}]},
    {"changed_files": [{"path": ".github/workflows/build.yml", "status": "modified", "binary": False}]},
    {"changed_files": [{"path": "src/image.dat", "status": "added", "binary": True}]},
    {"changed_files": [{"path": "safe.py", "old_path": "scripts/danger.sh", "status": "renamed", "binary": False}]},
    {"changed_files": [{"path": "../escape", "status": "modified", "binary": False}]},
    {"changed_files": [{"path": "sub/package.json", "status": "modified", "binary": False}]},
    {"changed_files": [{"path": "src/secret.py", "status": "modified", "binary": False}], "untrusted_forbidden_paths": ["src/secret.py"]},
])
def test_t25_changes_fail_before_any_runner_call(bad):
    runner = FakeRunner()
    result = run(request(**bad), runner)
    assert result["status"] == "parked"
    assert runner.prepared == runner.executed == []


@pytest.mark.parametrize("bad", [
    {"available": False}, {"isolation_verified": False}, {"home_absent": False},
    {"credentials_absent": False}, {"host_config_absent": False}, {"network_enforced": False},
    {"network_mode": "unrestricted"}, {"allow_hosts": ["other.example"]},
    {"sha": "c" * 40}, {"job_id": "unrelated"}, {"environment": "other"},
])
def test_t25_unconfirmed_isolation_never_executes(bad):
    runner = FakeRunner(prepare_override=bad)
    assert run(runner=runner)["status"] == "parked"
    assert runner.executed == []


def test_t25_two_stages_and_independent_workspaces():
    runner = FakeRunner()
    result = run(runner=runner)
    assert result["status"] == "passed"
    assert len(runner.executed) == 6
    groups = {}
    for invocation in runner.executed:
        groups.setdefault(invocation["attempt"], set()).add(invocation["workdir_id"])
        assert invocation["allow_hosts"] == (["registry.example"] if invocation["stage"] == "dependency" else [])
    assert len({next(iter(value)) for value in groups.values()}) == 3
    assert all(len(value) == 1 for value in groups.values())
    assert {i["subject_sha"] for i in runner.executed if i["revision_role"] == "head"} == {HEAD}
    assert len({i["job_id"] for i in runner.executed}) == 6


@pytest.mark.parametrize("hosts", [[], ["*"], ["127.0.0.1"], ["https://registry.example"], ["a..b"], ["1.2"]])
def test_t25_dependency_requires_current_explicit_allowlist(hosts):
    runner = FakeRunner()
    assert run(request(outbound_allowlist=hosts), runner)["status"] == "parked"
    assert not runner.executed


@pytest.mark.parametrize("change", [
    {"timed_out": True}, {"complete": False}, {"output_truncated": True},
    {"executed": False}, {"exit_code": None}, {"exit_code": True},
    {"ended_at": None}, {"started_at": "2026-11-08T00:00:00Z"},
])
def test_t26_incomplete_success_or_base_failure_is_never_exempt(change):
    runner = FakeRunner(result_override=change)
    result = run(runner=runner)
    assert result["status"] == "parked"
    assert result["reason"] == "evidence_incomplete"
    assert result["evidence"][-1]["complete"] is False


def test_t26_lost_names_and_new_failure_rejected():
    def remove_name(inv, result):
        if inv["revision_role"] == "head" and inv["stage"] == "verify":
            result["test_names"] = ["test_a"]
    assert run(runner=FakeRunner(result_override=remove_name))["reason"] == "test_names_missing"
    def new_failure(inv, result):
        if inv["revision_role"] == "head" and inv["stage"] == "verify":
            result.update(exit_code=1, failure_evidence={"test_b": "assertion differs"})
    assert run(runner=FakeRunner(result_override=new_failure))["reason"] == "new_failure"


def test_t26_both_attempts_must_pass_comparison():
    def fail_second(inv, result):
        if inv["attempt"] == "head-2" and inv["stage"] == "verify":
            result.update(exit_code=1, failure_evidence={"test_b": "new diagnostic"})
    assert run(runner=FakeRunner(result_override=fail_second))["status"] == "failed"
    assert run(request(attempts=["same", "same"]))["reason"] == "independent_attempts_required"


def test_t26_base_failure_needs_same_command_name_and_diagnostics():
    def existing(inv, result):
        if inv["stage"] == "verify":
            result.update(exit_code=1, failure_evidence={"test_a": "known diagnostic"})
    accepted = run(runner=FakeRunner(result_override=existing))
    assert accepted["status"] == "passed"
    assert all(c["reason"] == "base_derived_failure" and c["base_derived_failures"] == ["test_a"] for c in accepted["comparisons"])
    def different(inv, result):
        existing(inv, result)
        if inv["revision_role"] == "head" and inv["stage"] == "verify":
            result["failure_evidence"]["test_a"] = "changed diagnostic"
    assert run(runner=FakeRunner(result_override=different))["status"] == "failed"


def test_t26_failure_without_named_proof_is_not_excused():
    def unnamed(inv, result):
        if inv["stage"] == "verify":
            result.update(exit_code=1)
    assert run(runner=FakeRunner(result_override=unnamed))["reason"] == "new_failure"


def test_t26_result_identity_and_full_sha_required():
    assert run(request(head_sha="abc"))["reason"] == "full_sha_required"
    assert run(runner=FakeRunner(result_override={"sha": "c" * 40}))["reason"] == "result_identity_mismatch"


def test_t26_exact_complete_results_reused_without_counting_once_twice():
    cache = EvidenceCache()
    first = FakeRunner()
    assert run(runner=first, cache=cache)["status"] == "passed"
    second = FakeRunner()
    reused = run(runner=second, cache=cache)
    assert reused["status"] == "passed"
    assert second.executed == []
    assert len({e["execution_id"] for e in reused["evidence"]}) == 3
    assert {e["attempt"] for e in reused["evidence"] if e["revision_role"] == "head"} == {"head-1", "head-2"}


@pytest.mark.parametrize("field,value", [("head_sha", "c" * 40), ("base_sha", "d" * 40),
                                        ("repo", "example/other"), ("outbound_allowlist", ["new.example"])])
def test_t26_context_changes_invalidate_cache(field, value):
    cache = EvidenceCache()
    run(cache=cache)
    runner = FakeRunner()
    run(request(**{field: value}), runner, cache=cache)
    # A head change may retain the baseline; head results always rerun.
    assert len([i for i in runner.executed if i["revision_role"] == "head" and i["stage"] == "verify"]) == 2


@pytest.mark.parametrize("field,value", [("version", "2"), ("runner", "verify-node")])
def test_t26_recipe_setting_changes_invalidate_cache(field, value):
    cache = EvidenceCache()
    run(cache=cache)
    configured = recipes()
    configured["test"][field] = value
    runner = FakeRunner()
    result = run_verification(request(), runner, scrub=deepcopy, cache=cache, recipes=configured)
    assert result["status"] == "passed" and len(runner.executed) == 6


def test_t26_command_kind_attempt_changes_miss_cache():
    for changed in ("command", "kind", "attempt"):
        cache = EvidenceCache()
        run(cache=cache)
        configured = recipes()
        req = request()
        if changed == "command":
            configured["test"]["checks"][0]["command"] = "other-command"
        elif changed == "kind":
            configured["test"]["checks"][0]["kind"] = "unit"
        else:
            req["attempts"] = ["new-1", "new-2"]
        runner = FakeRunner()
        result = run_verification(req, runner, scrub=deepcopy, cache=cache, recipes=configured)
        assert result["status"] == "passed" and len(runner.executed) >= 4


def test_t26_incomplete_results_cannot_be_inserted():
    cache = EvidenceCache()
    run(cache=cache, runner=FakeRunner(result_override={"output_truncated": True}))
    assert cache._records == {}


def test_t26_mutating_returned_evidence_cannot_corrupt_checkpoint():
    cache = EvidenceCache()
    result = run(cache=cache)
    result["evidence"][-1]["complete"] = False
    assert run(cache=cache)["status"] == "passed"


def test_t26_all_evidence_fields_present_and_scrubbed():
    def scrub(obj):
        return json.loads(json.dumps(obj).replace("PRIVATE_MARKER", "[redacted]"))
    runner = FakeRunner(result_override={"output_tail": "PRIVATE_MARKER"})
    result = run_verification(request(), runner, recipes=recipes(), scrub=scrub)
    assert result["status"] == "passed"
    assert "PRIVATE_MARKER" not in json.dumps(result)
    required = {"repo", "base_sha", "head_sha", "recipe_version", "command", "validation_kind", "attempt",
                "started_at", "ended_at", "exit_code", "timed_out", "complete", "output_tail", "test_names", "environment"}
    assert all(required <= e.keys() and complete(e) for e in result["evidence"])


def test_t26_redacted_diagnostic_is_not_proof_of_same_failure():
    def scrub(obj):
        return json.loads(json.dumps(obj).replace("private diagnostic", "[redacted]"))
    def failure(inv, result):
        if inv["stage"] == "verify":
            result.update(exit_code=1, failure_evidence={"test_a": "private diagnostic"})
    result = run_verification(request(), FakeRunner(result_override=failure), recipes=recipes(), scrub=scrub)
    assert result["reason"] == "new_failure"


def test_runner_timeout_and_connection_loss_remain_local():
    class Timeout(FakeRunner):
        def execute(self, inv, prepared):
            raise TimeoutError()
    assert run(runner=Timeout())["reason"] == "evidence_incomplete"
    class Lost(FakeRunner):
        def execute(self, inv, prepared):
            raise ConnectionError("must not be recorded")
    result = run(runner=Lost())
    assert result["reason"] == "verify_unavailable"
    assert "must not be recorded" not in json.dumps(result)
    assert run()["status"] == "passed"


def test_hibi_server_unavailable_parks_only_this_job():
    req = request(test_recipe="verification/recipes.yaml#hibi-server")
    runner = FakeRunner(prepare_override={"entry_verified": False})
    result = run_verification(req, runner, scrub=deepcopy)
    assert result["status"] == "parked" and result["reason"] == "verify_unavailable"
    assert runner.executed == []
    assert run()["status"] == "passed"


def test_dreamforge_balance_and_full_paths_select_all_and_slow():
    req = request(test_recipe="dreamforge", untrusted=False, balance_changed=True)
    runner = FakeRunner()
    assert run_verification(req, runner, scrub=deepcopy)["status"] == "passed"
    kinds = {i["validation_kind"] for i in runner.executed}
    assert {"build", "generated", "changed", "all", "slow"} <= kinds
    for path in ("tools/test_changed.py", ".github/workflows/test.yml", "tools/balance/numbers.json"):
        runner = FakeRunner()
        req = request(test_recipe="dreamforge", untrusted=False,
                      changed_files=[{"path": path, "status": "modified", "binary": False}])
        assert run_verification(req, runner, scrub=deepcopy)["status"] == "passed"
        assert "all" in {i["validation_kind"] for i in runner.executed}


def test_hibi_unperformed_unit_e2e_and_missing_privacy_patterns():
    req = request(test_recipe="hibi", untrusted=False)
    result = run_verification(req, FakeRunner(), scrub=deepcopy)
    assert result["status"] == "passed" and result["unperformed"] == ["unit", "e2e"]
    result = run_verification(req, FakeRunner(prepare_override={"privacy_patterns_staged": False}), scrub=deepcopy)
    assert result["status"] == "parked" and result["reason"] == "privacy_patterns_unavailable"


def test_recipe_or_redactor_missing_never_succeeds():
    assert run(request(test_recipe="unknown"))["reason"] == "recipe_unconfigured"
    assert run_verification(request(), FakeRunner(), scrub=None)["reason"] == "redactor_unavailable"


def test_balance_directory_prefix_selects_slow():
    runner = FakeRunner()
    req = request(test_recipe="dreamforge", untrusted=False,
                  changed_files=[{"path": "Content/Balance/star.json", "status": "modified", "binary": False}])
    assert run_verification(req, runner, scrub=deepcopy)["status"] == "passed"
    assert {"all", "slow"} <= {i["validation_kind"] for i in runner.executed}


def test_redacted_test_names_cannot_hide_missing_tests():
    def scrub(obj):
        obj = deepcopy(obj)
        if obj.get("test_names"):
            obj["test_names"] = ["[redacted]"]
        return obj
    result = run_verification(request(), FakeRunner(), recipes=recipes(), scrub=scrub)
    assert result["status"] == "parked"
    assert result["reason"] == "evidence_incomplete"


@pytest.mark.parametrize("field,value", [("repo", 1), ("head_sha", "short"), ("base_sha", "short"),
                                        ("attempt", None), ("execution_id", 12), ("revision_role", "unknown")])
def test_t26_malformed_checkpoint_is_never_complete(field, value):
    evidence = run()["evidence"][-1]
    evidence[field] = value
    assert complete(evidence) is False


def test_example_recipe_loads_and_runs_via_public_interface(tmp_path, monkeypatch, repo_root):
    from plugins.subloop.change.verification import engine

    example = repo_root / "plugins/subloop/change/verification/recipes.example.yaml"
    (tmp_path / "recipes.yaml").write_bytes(example.read_bytes())
    monkeypatch.setattr(engine, "__file__", str(tmp_path / "engine.py"))
    runner = FakeRunner()
    result = run_verification(
        request(test_recipe="verification/recipes.yaml#example-dotnet"),
        runner, scrub=deepcopy,
    )
    assert result["status"] == "passed"
    assert any(inv["environment"] == "verify-dotnet" for inv in runner.executed)
    assert any(inv["validation_kind"] == "build" for inv in runner.executed)
