"""T-2: 退避（F-20、S-14.3、D-13）。偽の退避先（rrsync 付きの sshd の模擬。本物の rsync をローカルで使う）で通す。
実機の判定（subpath）と実際の archive-host は、ここでは通さない（status/b1.md の「実環境でしか通せない試験」）。"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from axcommon import timeutil
from core.archiver import config, manifest as mf, rsync as rs, subpath_probe
from core.archiver.config import Dest, Source
from core.archiver.engine import Archiver
from core.archiver.guard import Guard
from core.archiver.ledger import ArchiverLedger
from tests.fakes.b1.fake_remote import FakeRemote

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync が無い")
ROOT = Path(__file__).resolve().parents[2]
DEST = Dest("example.com", 2202, "user")
OLD_DAYS = 30


class Emit:
    def __init__(self):
        self.events = []

    def emit(self, kind, subject, data=None, **kw):
        ev = {"id": f"E{len(self.events)}", "kind": kind, "subject": subject, "data": data or {}}
        self.events.append(ev)
        return ev

    def kinds(self):
        return [e["kind"] for e in self.events]


class Cards:
    def __init__(self):
        self.calls = []

    def failure(self, source, reason, detail, *, first, event_id):
        self.calls.append((source, reason, first))
        return "created:X"


def old(path: Path, days=OLD_DAYS):
    t = timeutil.now().timestamp() - days * 86400
    os.utime(path, (t, t))


def put(root: Path, rel: str, data: bytes = b"hello", days=OLD_DAYS) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    old(p, days)
    return p


@pytest.fixture
def env(tmp_path, fake_clock, monkeypatch):
    stage_tmp = tmp_path / "stage-tmp"
    stage_tmp.mkdir()
    index = tmp_path / "redact-index"
    index.write_text("test-salt\n")
    pii = tmp_path / "pii-list"
    pii.write_text("")
    monkeypatch.setenv("TMPDIR", str(stage_tmp))
    monkeypatch.setenv("AX_REDACT_INDEX", str(index))
    monkeypatch.setenv("AX_PII_LIST", str(pii))
    state = tmp_path / "state"
    (state / "archive").mkdir(parents=True)
    remote = FakeRemote(tmp_path / "archive-host-root")
    emit, cards = Emit(), Cards()
    led = ArchiverLedger(tmp_path / "archiver.sqlite")

    class E:
        pass

    e = E()
    e.tmp, e.state, e.remote, e.emit, e.cards, e.led = tmp_path, state, remote, emit, cards, led

    def make(sources, guard=None, max_files=2000):
        tr = rs.Transport(DEST, remote.runner, key="/run/secrets/archive-ssh-key", known_hosts="/run/secrets/archive-known-hosts")
        return Archiver(sources, tr, led, emit, state_dir=state, guard=guard or Guard(), notifier=cards, max_files=max_files)

    e.make = make
    e.enable_reap = lambda: (state / "archive" / "reap-enabled").write_text("")
    return e


def vol(tmp, name="timeline-data", **kw):
    root = tmp / "vol" / name
    root.mkdir(parents=True, exist_ok=True)
    return Source(name=f"vol-{name}", path=str(root), root=root, min_age_days=kw.pop("min_age_days", 14), kind="volume",
                  keep_original=kw.pop("keep_original", config.VOLUME_KEEP_DEFAULT), remove_original=kw.pop("remove_original", True), **kw)


def host(tmp, name="mp-runs-archive", **kw):
    root = tmp / "src" / name
    root.mkdir(parents=True, exist_ok=True)
    return Source(name=name, path="/home/x/" + name, root=root, min_age_days=kw.pop("min_age_days", 14), kind="host", **kw)


def remote_file(e, src, rel, date=None):
    date = date or timeutil.now_ts()[:10]
    return e.remote.root / src.name / date / rel


# --- 転送の形（F-20）--------------------------------------------------------------------------
def test_copy_uses_checksum_files_from_and_never_delete_and_no_shell(env):
    src = host(env.tmp)
    put(src.root, "a/one.txt", b"1111")
    put(src.root, "two.txt", b"22")
    rep = env.make([src]).run_once()
    assert rep.sources[0].transferred == 2 and rep.sources[0].failed is None
    assert remote_file(env, src, "a/one.txt").read_bytes() == b"1111"
    rsyncs = [c for c in env.remote.commands if c[0] == "rsync"]
    assert rsyncs and all("--delete" not in " ".join(c) for c in rsyncs)
    data_cmds = [c for c in rsyncs if any(a.startswith("--files-from=") for a in c)]
    assert len(data_cmds) == 2                                   # 送信と、--dry-run --itemize-changes の再実行
    send, check = data_cmds
    assert "--checksum" in send and "-a" in send and "--dry-run" not in send
    assert "--dry-run" in check and "--itemize-changes" in check
    strip = lambda c: [("--files-from=LIST" if a.startswith("--files-from=") else a) for a in c if a not in ("--dry-run", "--itemize-changes")]
    assert strip(check) == strip(send)                           # 同じ引数
    e_arg = send[send.index("-e") + 1]
    assert "IdentitiesOnly=yes" in e_arg and "StrictHostKeyChecking=yes" in e_arg and "/run/secrets/archive-ssh-key" in e_arg
    assert "UserKnownHostsFile=/run/secrets/archive-known-hosts" in e_arg and "-p 2202" in e_arg
    assert Path(send[-2]).is_relative_to(env.tmp / "stage-tmp" / "ax-archiver")
    assert send[-1].startswith("user@example.com:mp-runs-archive/")
    assert env.remote.shell_attempts == []                       # シェルは使わない


def test_manifest_is_sent_next_to_the_files_and_event_is_written(env):
    src = host(env.tmp)
    put(src.root, "f.txt", b"abc")
    env.make([src]).run_once()
    date = timeutil.now_ts()[:10]
    m = json.loads((env.remote.root / src.name / date / f"{date}.json").read_text())
    assert m["name"] == src.name and m["files"][0]["path"] == "f.txt" and m["files"][0]["size"] == 3 and len(m["files"][0]["sha256"]) == 64
    ev = [e for e in env.emit.events if e["kind"] == "archive.moved"][0]
    assert ev["data"]["from"] == src.name and ev["data"]["bytes"] == 3 and ev["data"]["to"] == f"{src.name}/{date}"


def test_young_files_excluded_and_include_exclude_apply(env):
    src = host(env.tmp, min_age_days=14, include=("*.log", "*.txt"), exclude=("skip/*",))
    put(src.root, "old.log", b"x")
    put(src.root, "new.log", b"x", days=1)
    put(src.root, "skip/a.txt", b"x")
    put(src.root, "image.png", b"x")
    rep = env.make([src]).run_once()
    assert rep.sources[0].transferred == 1 and remote_file(env, src, "old.log").exists()
    assert not remote_file(env, src, "new.log").exists() and not remote_file(env, src, "image.png").exists()


def test_known_unchanged_files_are_not_sent_again_but_changed_ones_are(env):
    src = host(env.tmp)
    f = put(src.root, "f.txt", b"v1")
    a = env.make([src])
    assert a.run_once().sources[0].transferred == 1
    again = a.run_once().sources[0]
    assert again.transferred == 0 and again.candidates == 0
    f.write_bytes(b"v2-longer")
    old(f)
    assert a.run_once().sources[0].transferred == 1


def test_originals_are_never_touched_for_host_sources_by_the_archiver(env):
    src = host(env.tmp, remove_original=True)
    f = put(src.root, "f.txt", b"abc")
    env.enable_reap()
    env.make([src]).run_once()
    assert f.exists()                                            # ro の bind。除去はホスト側の reap


# --- 元の除去（volume の archive/。archiver 自身が移す）-----------------------------------------
def test_volume_copy_only_while_reap_marker_is_absent(env):
    src = vol(env.tmp)
    f = put(src.root, "timeline/old.jsonl", b"data")
    rep = env.make([src]).run_once().sources[0]
    assert rep.transferred == 1 and rep.removed == 0 and f.exists()          # 有効化の前はコピーだけ


def test_volume_removes_only_verified_unchanged_files_when_enabled(env):
    src = vol(env.tmp)
    f = put(src.root, "timeline/old.jsonl", b"data")
    idx = put(src.root, "timeline/index.json", b"{}")                        # 索引は残す
    env.enable_reap()
    rep = env.make([src]).run_once().sources[0]
    assert rep.transferred == 2 and rep.removed == 1 and rep.kept == 1
    assert not f.exists() and idx.exists()
    assert remote_file(env, src, "timeline/old.jsonl").read_bytes() == b"data"      # 退避先には届いている


def test_volume_keeps_a_file_rewritten_after_verification(env):
    src = vol(env.tmp)
    f = put(src.root, "a.dat", b"data")

    def rewrite():
        f.write_bytes(b"CHANGED-AFTER")
    env.remote.hook_after_send = None
    env.enable_reap()
    a = env.make([src])
    orig_verify = a.transport.verify

    def verify_then_rewrite(*args, **kw):
        orig_verify(*args, **kw)                                              # 照合は合格
        rewrite()                                                             # その後で元が書き換わる
    a.transport.verify = verify_then_rewrite
    rep = a.run_once().sources[0]
    assert rep.removed == 0 and rep.changed_after == 1 and f.read_bytes() == b"CHANGED-AFTER"
    assert env.led.count(src.name) == 0                                        # 「照合済み」にしない


# --- 内容の照合（T-2、T-78 の核）--------------------------------------------------------------------
def test_same_size_different_content_copy_keeps_the_original(env):
    src = vol(env.tmp)
    f = put(src.root, "a.dat", b"AAAAAAAA")
    env.enable_reap()
    env.remote.corrupt_on_send = True                            # 送信後に遠隔の中身を同じ大きさのまま変える
    rep = env.make([src]).run_once().sources[0]
    assert rep.failed == "verify_mismatch" and f.exists() and rep.removed == 0
    assert env.led.count(src.name) == 0
    assert "archive.failed" in env.emit.kinds() and "archive.moved" not in env.emit.kinds()
    remote_copy = remote_file(env, src, "a.dat").read_bytes()
    assert len(remote_copy) == len(b"AAAAAAAA") and remote_copy != b"AAAAAAAA"      # 件数も大きさも同じで、内容だけ違う


def test_verification_is_content_based_not_size_based(env):
    """--checksum の dry-run が、大きさと mtime が同じで内容だけ違うコピーを差分に出すこと（本物の rsync で確かめる）。"""
    src = host(env.tmp)
    f = put(src.root, "a.dat", b"AAAAAAAA")
    tr = rs.Transport(None, rs.subprocess_runner, dest_prefix=str(env.tmp / "dest"))
    tr.ensure_dirs("n/d")
    tr.send(src.root, ["a.dat"], "n/d")
    tr.verify(src.root, ["a.dat"], "n/d")                                      # 一致なら通る
    copy = env.tmp / "dest" / "n" / "d" / "a.dat"
    st = copy.stat()
    copy.write_bytes(b"BAAAAAAA")
    os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns))                         # 大きさも mtime も同じ
    with pytest.raises(rs.TransferError) as ei:
        tr.verify(src.root, ["a.dat"], "n/d")
    assert ei.value.reason == "verify_mismatch"


def test_parse_itemize_ignores_directory_attribute_lines_only():
    out = ".d..t...... a/\n>f.st...... a/x\n.f..t...... y\ncd+++++++++ z/\n\n"
    assert rs.parse_itemize(out) == [">f.st...... a/x", ".f..t...... y", "cd+++++++++ z/"]
    assert rs.parse_itemize(".d..t...... ./\n") == []


# --- 失敗の扱い --------------------------------------------------------------------------------
def test_unreachable_keeps_originals_holds_only_that_source_and_creates_one_card(env):
    s1 = vol(env.tmp, "a-data")
    s2 = host(env.tmp, "other")
    f1 = put(s1.root, "x.dat", b"1")
    f2 = put(s2.root, "y.dat", b"2")
    env.enable_reap()
    env.remote.down = True
    a = env.make([s1, s2])
    r = a.run_once()
    assert [s.failed for s in r.sources] == ["unreachable", "unreachable"] and f1.exists() and f2.exists()
    a.run_once()
    a.run_once()
    assert len([e for e in env.emit.events if e["kind"] == "archive.failed"]) == 2          # 退避元ごとに 1 件。毎巡は書かない
    assert sorted(env.cards.calls) == [("other", "unreachable", True), ("vol-a-data", "unreachable", True)]
    env.remote.down = False
    ok = a.run_once()
    assert all(s.failed is None for s in ok.sources) and env.led.failure("other", "unreachable")["count"] == 0
    assert env.led.count("other") == 1


def test_one_failing_source_does_not_stop_the_others(env):
    bad = host(env.tmp, "bad")
    good = host(env.tmp, "good")
    put(bad.root, "b.dat", b"b")
    put(good.root, "g.dat", b"g")
    a = env.make([bad, good])
    real = a.transport.send

    def send(src_root, paths, dest_rel):
        if dest_rel.startswith("bad/"):
            raise rs.TransferError("dest_permission", "no")
        return real(src_root, paths, dest_rel)
    a.transport.send = send
    r = a.run_once()
    assert r.sources[0].failed == "dest_permission" and r.sources[1].failed is None and remote_file(env, good, "g.dat").exists()


def test_disk_full_is_classified(env):
    src = host(env.tmp)
    put(src.root, "a", b"x" * 10)
    env.remote.full = True
    r = env.make([src]).run_once()
    assert r.sources[0].failed == "dest_full"


def test_classify_failure_table():
    assert rs.classify_failure(255, "ssh: connect to host x port 22: Connection refused") == "unreachable"
    assert rs.classify_failure(255, "x@y: Permission denied (publickey).") == "key_rejected"
    assert rs.classify_failure(255, "Host key verification failed.") == "key_rejected"
    assert rs.classify_failure(11, "write failed: No space left on device") == "dest_full"
    assert rs.classify_failure(23, "mkdir failed: Permission denied") == "dest_permission"
    assert rs.classify_failure(124, "") == "timeout"
    assert rs.classify_failure(1, "weird") == "rsync_error"


# --- 鍵の制限（rrsync）-------------------------------------------------------------------------
def test_restricted_key_cannot_write_outside_the_destination_or_run_a_shell(env):
    r = env.remote
    for target in ("user@example.com:../escape/", "user@example.com:/etc/", "user@example.com:a/../../escape/"):
        res = r.runner(["rsync", "-a", "--checksum", "-e", "ssh", str(env.tmp), target])
        assert res.rc == 12 and "outside" in res.stderr, target
    assert not (env.tmp / "escape").exists()
    assert r.exec_command("sh -c 'id'").rc == 1 and r.shell_attempts == ["sh -c 'id'"]
    assert r.runner(["ssh", "user@example.com", "ls"]).rc == 1


def test_archiver_destination_is_always_under_name_and_date(env):
    src = host(env.tmp, "n1")
    put(src.root, "a", b"x")
    env.make([src]).run_once()
    remotes = [a for c in env.remote.commands for a in c if re.match(r"^[\w.-]+@[\w.-]+:", a)]
    assert remotes and all(re.match(r"^user@example\.com:(n1/\d{4}-\d{2}-\d{2}/|)$", r) for r in remotes)


# --- 秘密置き場（S-14.3）-----------------------------------------------------------------------
def test_secrets_dir_symlinks_and_x_cookie_are_never_copied(env):
    secrets = env.tmp / "secrets"
    put(secrets, "key", b"TOPSECRET")
    src = host(env.tmp)
    put(src.root, "ok.txt", b"ok")
    (src.root / "link").symlink_to(secrets / "key")                         # 別名経由
    (src.root / "dirlink").symlink_to(secrets)                              # ディレクトリのリンク
    put(src.root, "x-cookie", b"c")
    put(src.root, "deep/x-cookie/c.txt", b"c")
    a = env.make([src], guard=Guard([secrets]))
    a.run_once()
    got = sorted(p.relative_to(env.remote.root / src.name / timeutil.now_ts()[:10]).as_posix()
                 for p in (env.remote.root / src.name).rglob("*") if p.is_file() and p.suffix != ".json")
    assert got == ["ok.txt"]
    assert not any("TOPSECRET" in p.read_text(errors="ignore") for p in env.remote.root.rglob("*") if p.is_file())


def test_source_inside_secrets_dir_is_refused_and_reported(env):
    secrets = env.tmp / "secrets"
    put(secrets, "archive/old.key", b"K")
    src = Source(name="bad", path=str(secrets / "archive"), root=secrets / "archive", min_age_days=1, kind="host")
    good = host(env.tmp, "good")
    put(good.root, "g", b"g")
    r = env.make([src, good], guard=Guard([secrets])).run_once()
    assert r.sources[0].refused and r.sources[0].failed == "secrets_dir_refused" and r.sources[1].failed is None
    assert not (env.remote.root / "bad").exists()


def test_source_root_that_is_a_symlink_into_secrets_is_refused(env):
    secrets = env.tmp / "secrets"
    put(secrets, "k", b"K")
    link_root = env.tmp / "src" / "alias"
    link_root.parent.mkdir(parents=True)
    link_root.symlink_to(secrets)
    src = Source(name="alias", path="/x", root=link_root, min_age_days=1, kind="host")
    r = env.make([src], guard=Guard([secrets])).run_once()
    assert r.sources[0].refused


# --- manifest と reap への受け渡し ------------------------------------------------------------
def test_host_manifest_goes_to_verified_only_when_remove_original(env):
    a_src = host(env.tmp, "keepme", remove_original=False)
    b_src = host(env.tmp, "reapme", remove_original=True, keep_original=("*/memory/**",))
    put(a_src.root, "f", b"1")
    put(b_src.root, "p/memory/MEMORY.md", b"m")
    put(b_src.root, "p/chat.jsonl", b"c")
    env.make([a_src, b_src]).run_once()
    date = timeutil.now_ts()[:10]
    v = env.state / "archive" / "verified"
    assert not (v / "keepme").exists()
    m = json.loads((v / "reapme" / f"{date}.json").read_text())
    byp = {f["path"]: f for f in m["files"]}
    assert byp["p/memory/MEMORY.md"].get("keep") is True and "keep" not in byp["p/chat.jsonl"]
    assert byp["p/chat.jsonl"]["inode"] == (b_src.root / "p/chat.jsonl").stat().st_ino
    from tests.b1.helpers import ext_errors
    assert ext_errors("archive-manifest", m) == []


# --- 設定 ----------------------------------------------------------------------------------------
def test_config_parses_sources_dest_and_volume_defaults(tmp_path):
    y = tmp_path / "sources.yaml"
    y.write_text("sources:\n  - {name: mp-runs-archive, path: ~/.claude/mp-runs/archive}\n"
                 "  - {name: claude-projects, path: ~/.claude/projects, min_age_days: 7, keep_original: ['*/memory/**'], remove_original: false}\n",
                 encoding="utf-8")
    (tmp_path / "vol" / "timeline-data").mkdir(parents=True)
    (tmp_path / "vol" / "Bad_Name").mkdir()
    src = config.load_sources(y, src_base="/src", vol_base=tmp_path / "vol", default_min_age_days=14)
    assert [s.name for s in src] == ["mp-runs-archive", "claude-projects", "vol-timeline-data"]
    assert src[0].min_age_days == 14 and src[1].min_age_days == 7 and src[1].keep_original == ("*/memory/**",)
    assert src[0].root == Path("/src/mp-runs-archive") and src[0].remove_original is False
    assert src[2].kind == "volume" and src[2].keep_original == config.VOLUME_KEEP_DEFAULT
    p = tmp_path / "hub-host.yaml"
    p.write_text("archive_dest: {host: archive-host.invalid, port: 2202, user: archive}\n", encoding="utf-8")
    assert config.load_dest(p, {}) == Dest("archive-host.invalid", 2202, "archive")
    assert config.load_dest(p, {"AX_ARCHIVE_DEST_PORT": "2203"}).port == 2203
    with pytest.raises(config.ConfigError):
        config.load_dest(None, {})
    for bad in ("sources: [{name: Bad, path: /x}]", "sources: [{name: vol-x, path: /x}]", "sources: [{name: a, path: rel}]",
                "sources: [{name: a, path: /x}, {name: a, path: /y}]"):
        with pytest.raises(config.ConfigError):
            config.parse_sources(__import__("yaml").safe_load(bad))
    assert config.parse_sources([{"name": "list-form", "path": "/x"}])[0].name == "list-form"          # 一覧だけのファイルも読む


def test_sources_file_schema():
    from tests.b1.helpers import ext_errors
    assert ext_errors("archive-sources", {"sources": [{"name": "a-b", "path": "~/x", "min_age_days": 7, "remove_original": False}]}) == []
    assert ext_errors("archive-sources", {"sources": [{"name": "A", "path": "x"}]})


def test_default_initial_sources_match_f20_table():
    """F-20 の初期値の表（存在しない物は登録しない）。profile/example.yaml 側の例は b0。ここでは名前と既定の年齢を決める定数を持たない。"""
    # 初期値の一覧は profile の archive-sources.yaml が持つ（git に置くのは例だけ）。コードに実パスを埋め込んでいないことを確かめる
    for p in (ROOT / "core" / "archiver").glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "/home/" not in text.replace("/home/x/", "") or p.name in ("__init__.py",), p.name
        assert ".claude/projects" not in text, p.name


# --- 削除の API が無い -----------------------------------------------------------------------------
def test_archiver_has_no_delete_api_and_never_passes_delete_to_rsync():
    for p in (ROOT / "core" / "archiver").glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert not re.search(r"""["']--(delete\w*|remove-source-files)["']""", text), p.name
    pytest.importorskip("starlette")
    from core.archiver import app as arch_app
    from tests.fakes.b1.fake_remote import FakeRemote  # noqa: F401
    tmp = Path(__import__("tempfile").gettempdir())
    ledger = ArchiverLedger(":memory:")
    a = Archiver([], rs.Transport(DEST, lambda *a, **k: rs.RunResult(0)), ledger, Emit(), state_dir=tmp)
    app = arch_app.build_app(a, run_heartbeat_loop=False)
    methods = {m for r in app.routes for m in (getattr(r, "methods", None) or [])}
    assert not (methods & {"DELETE", "PUT", "PATCH", "POST"})


# --- subpath の判定ロジック（実機の判定は未実行）-------------------------------------------------
def test_subpath_decision_logic():
    good = {"init_rc": 0, "listing": ["ax-subpath-probe.txt"], "parent_names_seen": [], "owner_sees_file": True}
    assert subpath_probe.evaluate(good).mode == "subpath"
    assert subpath_probe.evaluate(dict(good, init_rc=1)).mode == "named_volume"
    d = subpath_probe.evaluate(dict(good, parent_names_seen=["timeline.sqlite"]))
    assert d.mode == "named_volume" and d.reasons == ("only_archive_visible",)
    assert subpath_probe.evaluate(dict(good, listing=["other-file"])).mode == "named_volume"
    assert subpath_probe.evaluate(dict(good, owner_sees_file=False)).reasons == ("owner_sees_written_file",)
    assert subpath_probe.evaluate({}).mode == "named_volume"                    # 確かめられない物では subpath を採らない
    assert all(c[0] == "docker" for c in subpath_probe.plan("timeline-data"))


# --- ホスト側の除去 host/ax-archive-reap.sh（F-20、S-14.3、T-125、T-154 の (c)(d)）-------------------
REAP = ROOT / "host" / "ax-archive-reap.sh"


def reap(state, sources, *extra, check=True):
    p = subprocess.run(["bash", str(REAP), "--state-dir", str(state), "--sources", str(sources), *extra], capture_output=True, text=True)
    if check:
        assert p.returncode == 0, p.stderr
    return p


def sources_file(tmp, entries):
    import yaml
    f = tmp / "archive-sources.yaml"
    f.write_text(yaml.safe_dump({"sources": entries}), encoding="utf-8")
    return f


def setup_reap(env, remove=True, keep=()):
    s = host(env.tmp, "mp-runs-archive", remove_original=remove, keep_original=tuple(keep))
    files = {"a.txt": put(s.root, "a.txt", b"aaaa"), "sub/b.txt": put(s.root, "sub/b.txt", b"bbbb"), "p/memory/MEMORY.md": put(s.root, "p/memory/MEMORY.md", b"m")}
    env.make([s]).run_once()
    sf = sources_file(env.tmp, [{"name": s.name, "path": str(s.root), "remove_original": remove, "keep_original": list(keep)}])
    return s, files, sf


def test_reap_does_nothing_without_the_marker(env):
    s, files, sf = setup_reap(env)
    p = reap(env.state, sf)
    assert "reap-enabled が無い" in p.stdout and all(f.exists() for f in files.values())


def test_reap_removes_only_unchanged_verified_files_and_keeps_rewritten_and_keep(env):
    s, files, sf = setup_reap(env, keep=("*/memory/**",))
    files["sub/b.txt"].write_bytes(b"REWRITTEN-LATER")                         # 照合の後で書き換えた
    env.enable_reap()
    p = reap(env.state, sf)
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["removed"] == 1 and out["changed"] == 1 and out["keep"] == 1
    assert not files["a.txt"].exists() and files["sub/b.txt"].read_bytes() == b"REWRITTEN-LATER" and files["p/memory/MEMORY.md"].exists()
    p2 = reap(env.state, sf)                                                  # もう一度動かしても同じ（処理済みの印）
    assert json.loads(p2.stdout.strip().splitlines()[-1])["removed"] == 0
    assert (env.state / "archive" / "reap.log").read_text().count("\n") == 2


def test_reap_skips_sources_whose_remove_original_is_false_in_the_sources_file(env):
    s, files, sf = setup_reap(env, remove=True)
    sf = sources_file(env.tmp, [{"name": s.name, "path": str(s.root), "remove_original": False}])
    env.enable_reap()
    out = json.loads(reap(env.state, sf).stdout.strip().splitlines()[-1])
    assert out["removed"] == 0 and out["skipped_sources"] == [s.name] and all(f.exists() for f in files.values())


def test_reap_stops_when_marker_is_moved_midway(env):
    s, files, sf = setup_reap(env)
    # 印を作らずに動かす（止めた状態）→ 何も除去しない。印を作って動かし、途中で印が消えた状態は、除去する前の印の確認で模擬する
    p = reap(env.state, sf)
    assert all(f.exists() for f in files.values())
    env.enable_reap()
    marker = env.state / "archive" / "reap-enabled"
    # 実行中に印を移す: 偽の python3 は使わず、manifest を 2 つに分けて、1 つ目の処理中に印が消えることを sitecustomize で模擬するのは重いので、
    # 「印の確認が除去の直前に毎回ある」ことをスクリプトのソースで確かめる
    text = REAP.read_text(encoding="utf-8")
    assert text.index("os.path.lexists(marker)") < text.index("os.unlink(target)")
    marker.rename(env.tmp / "moved-marker")
    reap(env.state, sf)
    assert all(f.exists() for f in files.values())                             # 移した後は何も除去しない（削除はしていない）
    (env.tmp / "moved-marker").rename(marker)                                   # 戻す: manifest の再確認から再開する
    out = json.loads(reap(env.state, sf).stdout.strip().splitlines()[-1])
    assert out["removed"] == 3


def test_reap_never_leaves_the_source_root(env):
    s, files, sf = setup_reap(env)
    date = timeutil.now_ts()[:10]
    mp = env.state / "archive" / "verified" / s.name / f"{date}.json"
    doc = json.loads(mp.read_text())
    outside = env.tmp / "outside.txt"
    outside.write_bytes(b"x")
    st = outside.stat()
    doc["files"].append({"path": "../../outside.txt", "size": 1, "mtime_ns": st.st_mtime_ns, "inode": st.st_ino, "sha256": "0" * 64})
    mp.write_text(json.dumps(doc))
    env.enable_reap()
    reap(env.state, sf)
    assert outside.exists()


def test_reap_dry_run_changes_nothing(env):
    s, files, sf = setup_reap(env)
    env.enable_reap()
    out = json.loads(reap(env.state, sf, "--dry-run").stdout.strip().splitlines()[-1])
    assert out["removed"] == 3 and out["dry_run"] and all(f.exists() for f in files.values())
    assert not (env.state / "archive" / "reap.log").exists()


def test_reap_script_has_valid_shell_syntax_and_rejects_bad_args(env):
    assert subprocess.run(["bash", "-n", str(REAP)]).returncode == 0
    assert subprocess.run(["bash", str(REAP), "--state-dir", "relative/path"], capture_output=True).returncode == 1
    assert subprocess.run(["bash", str(REAP), "--bogus"], capture_output=True).returncode == 1


# --- /src の bind を作る断片 ------------------------------------------------------------------------
def test_gen_sources_compose_binds_existing_sources_read_only(tmp_path, capsys):
    from core.archiver import gen_sources_compose as g
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    y = tmp_path / "s.yaml"
    y.write_text("sources:\n  - {name: claude-projects, path: ~/.claude/projects, min_age_days: 7}\n  - {name: codex-runs, path: ~/.claude/codex-runs}\n",
                 encoding="utf-8")
    out = tmp_path / "gen.yaml"
    assert g.main(["--sources", str(y), "--home", str(home), "--out", str(out)]) == 0
    import yaml
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    vols = doc["services"]["archiver"]["volumes"]
    assert vols == [{"type": "bind", "source": str(home / ".claude" / "projects"), "target": "/src/claude-projects", "read_only": True}]
    assert "codex-runs" in capsys.readouterr().err                                    # 無い物は入れない
    bad = tmp_path / "bad.yaml"
    bad.write_text("sources: [{name: Bad, path: /x}]", encoding="utf-8")
    assert g.main(["--sources", str(bad), "--home", str(home)]) == 1
