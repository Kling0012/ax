from copy import deepcopy
import json
from pathlib import Path

import pytest

from plugins.subloop.change.verification import B6bRunner, EnvironmentRunner, RoutedRunner, run_verification
from plugins.subloop.change.verification.privacy import privacy_command
from tests.fakes.b14.verification.runner import FakeRunner

BASE = "a" * 40
HEAD = "b" * 40


def req(recipe):
    return {"repo": "example/project", "base_sha": BASE, "head_sha": HEAD,
            "test_recipe": recipe, "untrusted": True, "files_complete": True,
            "changed_files": [{"path": "src/code.py", "status": "modified", "binary": False}],
            "outbound_allowlist": ["registry.example"]}


class Launcher:
    def __init__(self):
        self.starts = []
        self.statuses = []

    def job_start(self, body):
        self.starts.append(body)
        return {"job_id": body["job_id"], "state": "running"}

    def job_status(self, job_id):
        self.statuses.append(job_id)
        return {"job_id": job_id, "state": "exited", "exit_code": 0}


def test_b6b_start_shape_uses_runner_and_current_stage_policy():
    launcher = Launcher()
    fake = FakeRunner()
    def collect(inv, poll):
        assert poll()["state"] == "exited"
        return fake.execute(inv, {})
    runner = B6bRunner(launcher, attest=fake.prepare, collect=collect)
    result = run_verification(req("hibi-memory"), runner, scrub=deepcopy)
    assert result["status"] == "passed"
    assert len(launcher.starts) == 6 and len(launcher.statuses) == 6
    for body in launcher.starts:
        assert set(body) == {"job_id", "runner", "allow_hosts", "budget_min", "workdir_id", "ro_mounts", "verify"}
        assert body["runner"] == "verify-python" and body["budget_min"] == 40
        assert body["verify"].startswith("cd /work/repo && ")
        assert "image" not in body and "network" not in body and "model" not in body


def test_env_fixed_entry_has_no_arbitrary_command_and_respects_env_binding():
    fake = FakeRunner()
    submissions = []
    def submit(body):
        submissions.append(body)
        return {"env_id": "example-local", "state": "running"}
    def collect(inv, poll):
        assert poll()["state"] == "succeeded"
        return fake.execute(inv, {})
    runner = EnvironmentRunner(submit=submit, status=lambda jid: {"state": "succeeded"}, attest=fake.prepare, collect=collect)
    result = run_verification(req("hibi-server"), RoutedRunner(None, runner), scrub=deepcopy)
    assert result["status"] == "passed"
    # All currently available checks plus an explicit independent full-suite record.
    assert len(submissions) == 6
    for body in submissions:
        assert body["entry"]["kind"] == "entrypoint"
        assert body["entry"]["name"] == "hibi-server-e2e"
        assert set(body["entry"]["args"]) == {"worktree"}
        assert body["budget_min"] == 40 and body["requires"] == ["docker"]
        assert body["irreversible"] is False and body["scope"] == "one"
        assert body["secrets"] == [] and body["outputs"] == ["e2e.log"]
        assert body["inputs"][0]["ref"].startswith("git:example/project@")
        assert "command" not in body


def test_unverified_env_never_submits():
    fake = FakeRunner(prepare_override={"entry_verified": False})
    submitted = []
    runner = EnvironmentRunner(submit=submitted.append, status=lambda jid: {}, attest=fake.prepare, collect=lambda *args: {})
    result = run_verification(req("hibi-server"), runner, scrub=deepcopy)
    assert result["status"] == "parked" and result["reason"] == "verify_unavailable"
    assert not submitted


def test_privacy_check_is_fixed_and_checks_missing_file_and_git_errors():
    command = privacy_command()
    assert "/work/privacy-patterns" in command
    assert "is_file" in command and "if not patterns:" in command
    assert '"git", "grep", "-qE", "--", pattern' in command
    assert "result.returncode != 1" in command


def test_evidence_and_result_schema_match_actual_records(repo_root):
    import jsonschema
    folder = repo_root / "schemas/ext/b14/verification"
    docs = [json.loads(path.read_text()) for path in folder.glob("*.json")]
    for doc in docs:
        jsonschema.Draft202012Validator.check_schema(doc)
    result = run_verification(req("hibi-server"), FakeRunner(), scrub=deepcopy)
    schema = json.loads((folder / "result.schema.json").read_text())
    resolver = jsonschema.RefResolver.from_schema(schema, store={doc["$id"]: doc for doc in docs})
    validator = jsonschema.Draft202012Validator(schema, resolver=resolver, format_checker=jsonschema.FormatChecker())
    assert not list(validator.iter_errors(result))
    incomplete = run_verification(req("hibi-server"), FakeRunner(result_override={"complete": False}), scrub=deepcopy)
    assert not list(validator.iter_errors(incomplete))
    request_schema = json.loads((folder / "request.schema.json").read_text())
    assert not list(jsonschema.Draft202012Validator(request_schema).iter_errors(req("hibi-server")))
