"""Exercise the retirement CLI through its public module entry point."""
import json
import os
import runpy
from pathlib import Path
import subprocess
import sys


def test_retirement_cli(tmp_path):
    role = runpy.run_path(str(Path(__file__).parents[3] / "tools/cutover/retirement/site.example.py"))["COMPUTE_ROLE"]
    base = [sys.executable, "-m", "tools.cutover.retirement.run", "--host", role]
    record = tmp_path / "plan.json"
    result = subprocess.run(base + ["--step", "all", "--dry-run", "--record", str(record)],
                            text=True, capture_output=True, check=True)
    plan = json.loads(result.stdout)
    assert json.loads(record.read_text()) == plan
    assert [s["step"] for s in plan["steps"]] == [f"G{i}" for i in range(1, 12)]
    assert plan["steps"][8]["status"] == "skipped"
    assert plan["steps"][8]["reason"] == "t142"
    assert all(isinstance(s["reversible"], bool) and "backup" in s for s in plan["steps"])
    assert plan["steps"][0]["commands"]
    blocked = subprocess.run(base + ["--step", "t142", "--dry-run"], text=True,
                             capture_output=True, check=True)
    assert json.loads(blocked.stdout) == {"status": "blocked", "reason": "ax-ci-runner not installed"}
    refused = subprocess.run(base + ["--step", "G1"], text=True, capture_output=True)
    assert refused.returncode == 2
    assert "not implemented" in refused.stderr


def test_profile_legacy_key_roundtrip():
    script = """
import json
from dataclasses import asdict
from tools.cutover.retirement import Host, Profile, PRECONDITIONS

def host(name, disk=None):
    return Host(name, '/home', '/user', '/system', '/sbin', '/fstab',
                '/bind', '/runtime', 1000, disk)

config = {'local': host('one'), 'legacy': host('two'),
          'legacy_role': host('three', '/disk'), 'day': '20300102',
          'preconditions': {key: True for key in PRECONDITIONS}}
profile = Profile(**config)
config["compute"] = host("preferred", "/disk")
preferred = Profile(**config)
print(json.dumps({"legacy": asdict(profile), "preferred": asdict(preferred)}))
"""
    env = dict(os.environ, AX_COMPUTE_LEGACY_KEYS="legacy_role")
    result = subprocess.run([sys.executable, "-c", script], env=env, text=True,
                            capture_output=True, check=True)
    saved = json.loads(result.stdout)
    assert saved["legacy"]["compute"]["name"] == "three"
    assert "legacy_role" not in saved["legacy"]
    assert saved["preferred"]["compute"]["name"] == "preferred"
    assert "Conflicting legacy compute profile key ignored" in result.stderr
