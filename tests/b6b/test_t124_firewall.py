"""T-124 環境への経路の制限（host/ax-fw.sh）。生成物の検査、冪等、控え、解決失敗で何も書かないこと。

ufw と iptables と tailscale は偽（tests/fakes/b6b/fake_fwtools.py）。after.rules は一時ファイル。実際の反映と到達の確認（resources のコンテナから環境のポートに届く、
他は届かない、archiver から archive-host の archive_dest のポートだけに届く）は実環境（status/b6b.md）。
"""
import json
import os
import subprocess
import sys

import yaml

from tests.b6b.helpers import ROOT

FW = ROOT / "host" / "ax-fw.sh"
TOOLS = ROOT / "tests" / "fakes" / "b6b" / "fake_fwtools.py"
RESOURCES, ARCHIVER = "198.51.100.11", "192.0.2.12"

BASE_RULES = """# rules.after\n*filter\n:ufw-after-input - [0:0]\n-A ufw-after-input -p udp --dport 137 -j ufw-skip-to-policy-input\nCOMMIT\n"""


class Fw:
    def __init__(self, tmp, targets=None, archive=None, ts=None):
        self.tmp = tmp
        self.after = tmp / "after.rules"
        self.after.write_text(BASE_RULES)
        self.archive = tmp / "archive"
        self.state = tmp / "fwstate"
        self.profile = tmp / "profile.yaml"
        self.targets = targets if targets is not None else [
            {"id": "env-a", "host": "host-a.invalid", "port": 22}, {"id": "env-b", "host": "host-b.invalid", "port": 2222}]
        self.dest = archive if archive is not None else {"host": "arch.invalid", "port": 2202, "user": "backup"}
        self.write_profile()
        self.tsmap = tmp / "tsmap.json"
        self.tsmap.write_text(json.dumps(ts or {"host-a.invalid": "192.0.2.10", "host-b.invalid": "192.0.2.11", "arch.invalid": "192.0.2.20", "": "192.0.2.1"}))
        py = sys.executable
        self.env = {
            **os.environ, "AX_HOME": str(tmp), "AX_FW_SUDO": "", "AX_FW_TAILSCALE": f"{py} {TOOLS} tailscale", "AX_FW_UFW": f"{py} {TOOLS} ufw",
            "AX_FW_IPTABLES": f"{py} {TOOLS} iptables", "AX_FW_OWNER": f"{os.getuid()}:{os.getgid()}",
            "FAKE_TS_MAP": str(self.tsmap), "FAKE_FW_STATE": str(self.state), "FAKE_AFTER_RULES": str(self.after), "TMPDIR": str(tmp),
            "AX_FW_RESOURCES_IP": RESOURCES, "AX_FW_ARCHIVER_IP": ARCHIVER, "AX_FW_WORKER_POOL_IP": "198.51.100.10",
        }

    def write_profile(self):
        prof = {"env_ssh_targets": self.targets}
        if self.dest is not None:
            prof["archive_dest"] = self.dest
        self.profile.write_text(yaml.safe_dump(prof))

    def run(self, cmd, *extra, env=None):
        e = dict(self.env)
        e.update(env or {})
        return subprocess.run(["bash", str(FW), cmd, "--profile", str(self.profile), "--after-rules", str(self.after), "--archive-dir", str(self.archive), *extra],
                              capture_output=True, text=True, env=e)

    def calls(self):
        p = self.state / "calls.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def block_lines(out):
    return [l for l in out.splitlines() if l.startswith("-A DOCKER-USER")]


def test_t124_rules_generated_from_profile(tmp_path):
    fw = Fw(tmp_path)
    r = fw.run("rules")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "# BEGIN AX" and lines[-1] == "# END AX"
    assert "*filter" in lines and ":DOCKER-USER - [0:0]" in lines and lines[-2] == "COMMIT"
    assert block_lines(r.stdout) == [
        f"-A DOCKER-USER -i ax-work -s {RESOURCES} -o tailscale0 -d 192.0.2.10 -p tcp --dport 22 -j ACCEPT",
        f"-A DOCKER-USER -i ax-work -s {RESOURCES} -o tailscale0 -d 192.0.2.11 -p tcp --dport 2222 -j ACCEPT",
        f"-A DOCKER-USER -i ax-core -s {ARCHIVER} -o tailscale0 -d 192.0.2.20 -p tcp --dport 2202 -j ACCEPT",
        "-A DOCKER-USER -i ax-core -o tailscale0 -j DROP",
        "-A DOCKER-USER -i ax-work -o tailscale0 -j DROP",
        "-A DOCKER-USER -i ax-job -o tailscale0 -j DROP",
        "-A DOCKER-USER -j RETURN",
    ]
    assert fw.after.read_text() == BASE_RULES  # rules は何も書かない


def test_t124_accept_rules_only_from_resources_and_archiver_exception_only_one(tmp_path):
    out = Fw(tmp_path).run("rules").stdout
    accepts = [l for l in block_lines(out) if l.endswith("-j ACCEPT")]
    assert len(accepts) == 3
    assert all(f"-s {RESOURCES}" in l for l in accepts[:2]) and f"-s {ARCHIVER}" in accepts[2]
    assert sum("-i ax-core" in l for l in accepts) == 1   # ax-core から出られるのは archiver の 1 本だけ
    assert not any("-i ax-job" in l for l in accepts)     # ax-job には許可が無い
    drops = [l for l in block_lines(out) if l.endswith("DROP")]
    assert {l.split()[3] for l in drops} == {"ax-core", "ax-work", "ax-job"}
    # 許可は DROP より前、RETURN は最後
    idx = {l: i for i, l in enumerate(block_lines(out))}
    assert max(idx[l] for l in accepts) < min(idx[l] for l in drops)
    assert block_lines(out)[-1].endswith("RETURN")
    assert not any("0.0.0.0" in l for l in block_lines(out))


def test_t124_no_archive_dest_means_no_archiver_exception(tmp_path):
    fw = Fw(tmp_path, archive=None)
    fw.dest = None
    fw.write_profile()
    out = fw.run("rules").stdout
    assert ARCHIVER not in out and "-d 192.0.2.20" not in out
    assert len([l for l in block_lines(out) if l.endswith("ACCEPT")]) == 2


def test_t124_apply_is_idempotent_and_takes_a_backup_first(tmp_path):
    fw = Fw(tmp_path)
    r = fw.run("apply")
    assert r.returncode == 0, r.stderr
    new = fw.after.read_text()
    assert new.startswith(BASE_RULES.rstrip("\n")) and "# BEGIN AX" in new and new.rstrip().endswith("# END AX")
    backups = sorted(fw.archive.glob("after.rules.*"))
    assert len(backups) == 1 and backups[0].read_text() == BASE_RULES      # 書き換える前の内容の控え
    assert list(fw.archive.glob("DOCKER-USER.*.txt"))                       # iptables -S の保存
    assert [c for c in fw.calls() if c[0] == "ufw"] == [["ufw", "reload"]]
    # 2 回目: 同じ結果。控えも reload も増えない
    r2 = fw.run("apply")
    assert r2.returncode == 0 and "変更なし" in r2.stdout
    assert fw.after.read_text() == new
    assert len(list(fw.archive.glob("after.rules.*"))) == 1
    assert [c for c in fw.calls() if c[0] == "ufw"] == [["ufw", "reload"]]
    assert fw.run("check").returncode == 0


def test_t124_apply_replaces_only_the_ax_block_and_keeps_the_rest(tmp_path):
    fw = Fw(tmp_path)
    fw.after.write_text(BASE_RULES + "# BEGIN AX\nold stuff\n# END AX\n# trailer\n*nat\nCOMMIT\n")
    assert fw.run("apply").returncode == 0
    t = fw.after.read_text()
    assert "old stuff" not in t and t.count("# BEGIN AX") == 1 and t.count("# END AX") == 1
    assert t.startswith(BASE_RULES) and t.rstrip().endswith("COMMIT") and "# trailer\n*nat\nCOMMIT" in t
    assert fw.run("apply").stdout.strip().endswith("変更なし")
    # 環境を台帳に足したとき（profile の変更）も同じスクリプトで更新できる
    fw.targets.append({"id": "env-c", "host": "host-c.invalid", "port": 22})
    fw.write_profile()
    fw.tsmap.write_text(json.dumps({"host-a.invalid": "192.0.2.10", "host-b.invalid": "192.0.2.11", "host-c.invalid": "192.0.2.12", "arch.invalid": "192.0.2.20", "": "192.0.2.1"}))
    assert fw.run("check").returncode == 1                     # 違いを検出
    assert fw.run("apply").returncode == 0
    assert "-d 192.0.2.12 -p tcp --dport 22" in fw.after.read_text()
    assert len(list(fw.archive.glob("after.rules.*"))) == 2     # 書き換えのたびに 1 つ控える（上書きしない）


def test_t124_unresolvable_host_writes_nothing(tmp_path):
    fw = Fw(tmp_path, ts={"host-a.invalid": "192.0.2.10", "arch.invalid": "192.0.2.20"})  # host-b が解決できない
    r = fw.run("apply")
    assert r.returncode == 1 and "env-b" in r.stderr
    assert fw.after.read_text() == BASE_RULES and not fw.archive.exists()
    assert [c for c in fw.calls() if c[0] == "ufw"] == []
    fw2 = Fw(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, ts={"host-a.invalid": "192.0.2.10", "host-b.invalid": "192.0.2.11"})  # archive_dest が解決できない
    assert fw2.run("apply").returncode == 1


def test_t124_broken_markers_refuse_to_write(tmp_path):
    for bad in ("# BEGIN AX\nno end\n", "# END AX\n# BEGIN AX\n", "# BEGIN AX\n# END AX\n# BEGIN AX\n# END AX\n"):
        fw = Fw(tmp_path / ("m%d" % abs(hash(bad)))) if (tmp_path / ("m%d" % abs(hash(bad)))).mkdir() is None else None
        fw.after.write_text(bad)
        r = fw.run("apply")
        assert r.returncode == 1 and "印が壊れている" in r.stderr
        assert fw.after.read_text() == bad and not fw.archive.exists()


def test_t124_profile_validation(tmp_path):
    for targets in ([{"id": "a", "host": "h;rm", "port": 22}], [{"id": "a", "host": "h.invalid", "port": 0}], [{"id": "a", "host": "h.invalid"}],
                    [{"id": "a", "host": "h.invalid", "port": 22}, {"id": "a", "host": "h2.invalid", "port": 22}], "notalist"):
        fw = Fw(tmp_path / ("p%d" % abs(hash(str(targets)))), targets=targets) if (tmp_path / ("p%d" % abs(hash(str(targets))))).mkdir() is None else None
        r = fw.run("rules")
        assert r.returncode == 1, targets
        assert r.stdout == ""
    fw = Fw(tmp_path)
    fw.profile.write_text("not: [valid")
    assert fw.run("rules").returncode == 1
    fw.profile.unlink()
    assert fw.run("rules").returncode == 1


def test_t124_reload_failure_is_reported_but_backup_exists(tmp_path):
    fw = Fw(tmp_path)
    r = fw.run("apply", env={"FAKE_UFW_FAIL": "reload"})
    assert r.returncode == 3 and "reload に失敗" in r.stderr
    assert list(fw.archive.glob("after.rules.*"))
    r = fw.run("apply", "--no-reload")
    assert r.returncode == 0  # 内容は同じ。reload しない


def test_t124_no_reload_flag_skips_ufw(tmp_path):
    fw = Fw(tmp_path)
    assert fw.run("apply", "--no-reload").returncode == 0
    assert [c for c in fw.calls() if c[0] == "ufw"] == []


def test_t124_apply_creates_after_rules_if_missing(tmp_path):
    fw = Fw(tmp_path)
    fw.after.unlink()
    assert fw.run("apply", "--no-reload").returncode == 0
    assert fw.after.read_text().startswith("# BEGIN AX")
    assert not list(fw.archive.glob("after.rules.*"))  # 元が無いので控えも無い


def test_t124_ufw_rules_list(tmp_path):
    r = Fw(tmp_path).run("ufw-rules")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "ufw allow in on ax-core to any port 3425 proto tcp",
        "ufw allow in on ax-work to any port 3425 proto tcp",
        "ufw allow in on ax-work from 198.51.100.10 to any port 22 proto tcp",
        f"ufw allow in on ax-work from {RESOURCES} to any port 22 proto tcp",
        "ufw allow in on ax-core to 192.0.2.1 port 8377 proto tcp",
        "ufw allow in on ax-core to 192.0.2.1 port 8908 proto tcp",
    ]
    assert not any("ax-job" in l for l in r.stdout.splitlines())  # ax-job には開けない


def test_t124_ufw_apply_saves_status_first_then_allows(tmp_path):
    fw = Fw(tmp_path)
    r = fw.run("ufw-apply")
    assert r.returncode == 0, r.stderr
    assert list(fw.archive.glob("ufw-status-numbered.*.txt"))
    calls = [c for c in fw.calls() if c[0] == "ufw"]
    assert calls[0] == ["ufw", "status", "numbered"] and len(calls) == 7
    assert all(c[1] == "allow" for c in calls[1:])
    assert calls[1] == ["ufw", "allow", "in", "on", "ax-core", "to", "any", "port", "3425", "proto", "tcp"]


def test_t124_script_has_no_hard_coded_real_addresses_or_secrets():
    text = FW.read_text()
    import re

    ips = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text))
    assert ips <= {"198.51.100.11", "192.0.2.12", "198.51.100.10"}  # compose の固定アドレスだけ。tailnet のアドレスは入れない
    import ipaddress
    assert all(ipaddress.ip_address(ip) not in ipaddress.ip_network((100 << 24 | 64 << 16, 10)) for ip in ips)


def test_t124_missing_address_stops_only_rule_generation(tmp_path):
    fw = Fw(tmp_path)
    result = fw.run("rules", env={"AX_FW_RESOURCES_IP": ""})
    assert result.returncode != 0
    assert "AX_FW_RESOURCES_IP" in result.stderr
    assert fw.after.read_text() == BASE_RULES
    assert fw.calls() == []


def test_t124_reads_launcher_env_without_executing_it(tmp_path):
    fw = Fw(tmp_path)
    config = tmp_path / 'config'
    config.mkdir()
    marker = tmp_path / 'should-not-exist'
    (config / 'launcher.env').write_text(
        f'AX_FW_RESOURCES_IP={RESOURCES}\n'
        f'AX_FW_ARCHIVER_IP={ARCHIVER}\n'
        'AX_FW_WORKER_POOL_IP=198.51.100.10\n'
        f'UNRELATED=$(touch {marker})\n'
    )
    env = {'AX_HOME': str(tmp_path), 'AX_FW_RESOURCES_IP': '',
           'AX_FW_ARCHIVER_IP': '', 'AX_FW_WORKER_POOL_IP': ''}
    result = fw.run('rules', env=env)
    assert result.returncode == 0, result.stderr
    assert RESOURCES in result.stdout and ARCHIVER in result.stdout
    assert not marker.exists() and fw.after.read_text() == BASE_RULES
    (config / 'launcher.env').write_text('AX_FW_RESOURCES_IP=invalid\n')
    result = fw.run('rules', env=env)
    assert result.returncode != 0 and fw.after.read_text() == BASE_RULES
