"""ax-env-run の B6 最小版の本体（F-18.2、S-8.11、ENV-MODULE §5）。launcher。

対象ホストの専用ユーザーとして動く。worker-pool から SSH の強制コマンド（--caller launcher）で呼ばれ、
`sudo -n docker run` でジョブ用コンテナを起動する。サブコマンドは job-start / job-status / job-stop / job-retire / job-list だけ。
標準ライブラリと PyYAML だけ。出力は 1 行の JSON。値（秘密）は出力・ログ・コマンド行に出さない。

終了コード（ENV-MODULE §5）: 0 成功 / 2 依頼の不備（rejected） / 3 見つからない / 4 満枠（busy） / 5 取り外し済み / 70 内部の失敗。

呼び出し元が指定できるのは runner の名前、モデル、許可先のホスト名、時間、環境変数の名前、target_ro、登録簿の id（ro_mounts）、
session_id、resume_from、workdir_id（と verify・tools）だけ。イメージ、マウントのパス、ネットワーク、コマンドは指定できない。
未知のキーはすべて拒否する。

差し替え点（試験用。強制コマンドの下では sshd が環境変数を通さない。restrict）:
  AX_ENV_HOME   ~/.ax-env（このファイルの位置から引く）
  AX_HOME       ~/.local/share/ax/prod（config/runners/、config/ro-mounts.yaml の場所）
  AX_PROFILE    明示した profile の場所（AX_PROFILE_FILE と既定値より優先）
  AX_PROFILE_FILE ~/.config/ax/profile/default.yaml（secrets_dir、tmp_dir、ax_state_dir、sandbox_gate_port）
  AX_WORK_DIR   ~/.local/share/ax/work（F-18.1 の作業置き場）
  AX_ENV_SUDO   "sudo -n"（空なら sudo なし）
  AX_ENV_DOCKER "docker"
  AX_JOB_NETWORK "ax-job"
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import ipaddress
import json
import os
import re
import shlex
import stat
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

EXIT_OK, EXIT_REJECTED, EXIT_NOT_FOUND, EXIT_BUSY, EXIT_DETACHED, EXIT_INTERNAL = 0, 2, 3, 4, 5, 70

ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
UUID_RE = re.compile(r"^[0-9a-f-]{36}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,120}$")
HOST_RE = re.compile(r"^[a-z0-9.-]+$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
IPV4_RE = re.compile(r"^[0-9.]+$")

LAUNCHER_SUBCOMMANDS = {"job-start", "job-status", "job-stop", "job-retire", "job-list", "job-log", "job-snapshot", "job-report", "job-verify-recipe", "job-verify-prepare", "job-verify-result", "job-verify-expire", "job-chat-result"}
# ENV-MODULE §5 のサブコマンド（--caller env の分。B14b が実装する。B6 の最小版は実装しない）
ENV_SUBCOMMANDS = {"submit", "status", "logs", "fetch", "cancel", "lookup", "release", "secret-put", "probe", "validate", "version"}
CALLERS = {"launcher", "env"}

# 起動時に runner の env で上書きさせない名前（launcher が固定する）
RESERVED_ENV = {"TMPDIR", "HOME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"}
# 追加マウントで重ねてはいけない container_path（F-11.5）
RESERVED_CONTAINER_PATHS = ["/work", "/tmp-hdd", "/target", "/tmp"]
PROXY_HOST = "egress-proxy"
PROXY_PORT = 3128
GATEWAY_PORT = 3425
AGENT_SETTINGS = {"promptCacheTtl": "1h", "subagentPromptCacheTtl": "1h"}
KINDS_WITH_MODEL = {"agent-cli", "chat-json"}


class LauncherError(Exception):
    def __init__(self, code: int, cls: str, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.code, self.cls, self.reason, self.message = code, cls, reason, message or reason

    def as_json(self) -> dict:
        return {"ok": False, "error": {"class": self.cls, "reason": self.reason, "message": self.message}}


def rejected(reason: str, message: str = "") -> LauncherError:
    return LauncherError(EXIT_REJECTED, "rejected", reason, message)


def not_found(reason: str, message: str = "") -> LauncherError:
    return LauncherError(EXIT_NOT_FOUND, "not_found", reason, message)


def busy(reason: str = "busy", message: str = "") -> LauncherError:
    return LauncherError(EXIT_BUSY, "busy", reason, message)


def internal(reason: str, message: str = "") -> LauncherError:
    return LauncherError(EXIT_INTERNAL, "internal", reason, message)


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_yaml(path: Path):
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise internal("pyyaml_missing", "PyYAML が無い") from e
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise internal("config_unreadable", f"{path.name}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise internal("config_invalid", f"{path.name} が YAML でない") from e


def _atomic_write(path: Path, data: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)  # 自分が作った一時ファイルだけ
        except OSError:
            pass
        raise


class Config:
    """環境変数と profile から引く場所。profile の値は読むだけ（アドレスを埋め込まない）。"""

    def __init__(self, environ: dict | None = None):
        env = dict(os.environ if environ is None else environ)
        self.env = env
        home = Path(env.get("HOME", "~")).expanduser()
        here = Path(__file__).resolve()
        # <env_home>/releases/<ver>/lib/ax_launcher.py
        self.env_home = Path(env.get("AX_ENV_HOME") or (here.parents[3] if len(here.parents) > 3 else home / ".ax-env")).expanduser()
        self.ax_home = Path(env.get("AX_HOME") or home / ".local/share/ax/prod").expanduser()
        self.profile_path = Path(env.get("AX_PROFILE") or env.get("AX_PROFILE_FILE") or home / ".config/ax/profile/default.yaml").expanduser()
        self.work_dir = Path(env.get("AX_WORK_DIR") or home / ".local/share/ax/work").expanduser()
        self.sudo = shlex.split(env["AX_ENV_SUDO"]) if "AX_ENV_SUDO" in env else ["sudo", "-n"]
        self.docker = shlex.split(env.get("AX_ENV_DOCKER", "docker"))
        self.network = env.get("AX_JOB_NETWORK", "ax-job")
        self.home = home
        self._profile: dict | None = None

    @property
    def profile(self) -> dict:
        if self._profile is None:
            data = _load_yaml(self.profile_path)
            if not isinstance(data, dict):
                raise internal("profile_invalid", "profile の最上位がマッピングでない")
            self._profile = data
        return self._profile

    def _profile_path(self, key: str) -> Path:
        v = self.profile.get(key)
        if not isinstance(v, str) or not v:
            raise internal("profile_missing", f"profile に {key} が無い")
        p = Path(os.path.expanduser(v))
        if not p.is_absolute():
            raise internal("profile_invalid", f"profile の {key} が相対パス")
        return p

    @property
    def secrets_dir(self) -> Path:
        return self._profile_path("secrets_dir")

    @property
    def tmp_dir(self) -> Path:
        return self._profile_path("tmp_dir")

    @property
    def state_dir(self) -> Path:
        v = self.profile.get("ax_state_dir")
        if not isinstance(v, str) or not v:
            raise internal("profile_missing", "profile に ax_state_dir が無い")
        p = Path(os.path.expanduser(v))
        if not p.is_absolute():
            raise internal("profile_invalid", "profile の ax_state_dir が相対パス")
        return p

    @property
    def gate_port(self) -> int:
        v = self.profile.get("sandbox_gate_port", 8931)
        if not isinstance(v, int) or not 1 <= v <= 65535:
            raise internal("profile_invalid", "profile の sandbox_gate_port が不正")
        return v

    # --- 派生 ---
    @property
    def jobs_dir(self) -> Path:
        return self.state_dir / "jobs"

    @property
    def egress_dir(self) -> Path:
        return self.state_dir / "egress"

    def work_of(self, wd: str) -> Path:
        return self.work_dir / f"job-{wd}"

    def chain_of(self, root: str) -> Path:
        return self.tmp_dir / f"job-{root}"


# ---------------------------------------------------------------- docker の呼び出し
class Docker:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _argv(self, args: list[str], preserve: list[str] | None = None) -> list[str]:
        sudo = list(self.cfg.sudo)
        if sudo and preserve:
            sudo.append("--preserve-env=" + ",".join(preserve))
        return sudo + self.cfg.docker + args

    def run(self, args: list[str], *, env: dict | None = None, preserve: list[str] | None = None, timeout: int = 120,
            merge_stderr: bool = False) -> subprocess.CompletedProcess:
        full = dict(self.cfg.env)
        full.update(env or {})
        try:
            return subprocess.run(
                self._argv(args, preserve), env=full, capture_output=not merge_stderr, text=True, timeout=timeout,
                stdout=subprocess.PIPE if merge_stderr else None, stderr=subprocess.STDOUT if merge_stderr else None,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise internal("docker_unavailable", f"docker を呼べない: {type(e).__name__}") from e

    def ps_ids(self, label: str) -> list[str]:
        r = self.run(["ps", "-q", "--filter", label, "--filter", "status=running"])
        if r.returncode != 0:
            raise internal("docker_ps_failed", "docker ps が失敗した")
        return [x for x in r.stdout.split() if x]

    def inspect(self, name: str) -> dict | None:
        r = self.run(["inspect", "--type", "container", name])
        if r.returncode != 0:
            return None
        try:
            data = json.loads(r.stdout)
        except ValueError as e:
            raise internal("docker_inspect_invalid", "docker inspect の出力が JSON でない") from e
        return data[0] if isinstance(data, list) and data else None


# ---------------------------------------------------------------- 依頼の検査
def _is_ip_like(host: str) -> bool:
    if IPV4_RE.match(host):
        return True
    return ":" in host


def parse_request(text: str, job_id_arg: str) -> dict:
    try:
        req = json.loads(text)
    except ValueError as e:
        raise rejected("bad_json", "標準入力が JSON でない") from e
    if not isinstance(req, dict):
        raise rejected("bad_json", "依頼が JSON のオブジェクトでない")
    allowed = {"job_id", "runner", "model", "allow_hosts", "budget_min", "env_names", "target_ro", "ro_mounts",
               "session_id", "resume_from", "workdir_id", "verify", "tools", "prompt", "snapshot_id", "safety_profile"}
    unknown = sorted(set(req) - allowed)
    if unknown:
        # image / mounts / network / command / host_path などをここで落とす（F-18.2、T-40、T-118）
        raise rejected("unknown_field", "指定できない項目: " + ", ".join(unknown[:8]))
    for k in ("job_id", "runner", "allow_hosts", "budget_min"):
        if k not in req:
            raise rejected("missing_field", f"{k} が無い")
    if req["job_id"] != job_id_arg:
        raise rejected("job_id_mismatch", "引数の job_id と依頼の job_id が違う")

    def need(key, cond, why):
        v = req.get(key)
        if v is not None and not cond(v):
            raise rejected("bad_field", f"{key}: {why}")

    if not isinstance(req.get("prompt"), str) or not req["prompt"].strip() or len(req["prompt"].encode("utf-8")) > 131072:
        raise rejected("bad_prompt", "prompt must be nonempty and at most 131072 bytes")
    need("job_id", lambda v: isinstance(v, str) and ID_RE.match(v), "[a-z0-9-]{1,40}")
    need("runner", lambda v: isinstance(v, str) and ID_RE.match(v), "[a-z0-9-]{1,40}")
    need("model", lambda v: isinstance(v, str) and MODEL_RE.match(v), "モデル名の形でない")
    need("budget_min", lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 1440, "1〜1440 の整数")
    need("session_id", lambda v: isinstance(v, str) and UUID_RE.match(v), "UUID の形でない")
    need("resume_from", lambda v: isinstance(v, str) and ID_RE.match(v), "[a-z0-9-]{1,40}")
    need("snapshot_id", lambda v: isinstance(v, str) and ID_RE.fullmatch(v), "snapshot id")
    if req.get("snapshot_id") and ("target_ro" in req or "ro_mounts" in req):
        raise rejected("bad_field", "snapshot cannot be combined with other mounts")
    need("workdir_id", lambda v: isinstance(v, str) and ID_RE.match(v), "[a-z0-9-]{1,40}")
    need("safety_profile", lambda v: v == "tool-free-chat-v1", "unknown safety profile")
    need("verify", lambda v: isinstance(v, str) and 0 < len(v) <= 8192, "1〜8192 文字の文字列")
    need("target_ro", lambda v: isinstance(v, str) and 0 < len(v) <= 1024, "文字列")
    for key, pat, mx in (("allow_hosts", None, 64), ("env_names", ENV_NAME_RE, 16), ("ro_mounts", ID_RE, 3), ("tools", None, 4)):
        v = req.get(key)
        if v is None:
            continue
        if not isinstance(v, list) or len(v) > mx or not all(isinstance(x, str) for x in v):
            raise rejected("bad_field", f"{key}: 文字列の配列（最大 {mx}）")
        if pat is not None and not all(pat.match(x) for x in v):
            raise rejected("bad_field", f"{key}: 形が違う要素がある")
    for h in req["allow_hosts"]:
        if not HOST_RE.match(h) or len(h) > 253 or _is_ip_like(h) or h.startswith(".") or ".." in h or "*" in h:
            raise rejected("bad_allow_host", "allow_hosts は FQDN だけ（IP アドレスと * は不可）")
    for t in req.get("tools") or []:
        if t not in ("sandbox-desktop", "web-gui", "web-screenshot", "linux-sandbox"):
            raise rejected("bad_field", "tools: 知らない能力名")
    return req


# ---------------------------------------------------------------- runner と登録簿
def load_runner(cfg: Config, name: str) -> dict:
    path = cfg.ax_home / "config" / "runners" / f"{name}.yaml"
    if not path.is_file():
        raise rejected("unknown_runner", "その runner の定義が無い")
    data = _load_yaml(path)
    if not isinstance(data, dict):
        raise internal("runner_invalid", "runner の定義が不正")
    for k in ("name", "kind", "image", "command"):
        if not isinstance(data.get(k), str) or not data[k]:
            raise internal("runner_invalid", f"runner の定義に {k} が無い")
    if data["name"] != name:
        raise internal("runner_invalid", "runner の name がファイル名と違う")
    if data["kind"] not in ("agent-cli", "chat-json", "verify"):
        raise internal("runner_invalid", "runner の kind が不正")
    env = data.get("env") or {}
    if not isinstance(env, dict) or any(k in RESERVED_ENV for k in env):
        raise internal("runner_invalid", "runner の env が不正（予約済みの名前は使えない）")
    return data


def read_current_env(cfg: Config) -> dict:
    p = cfg.state_dir / "deploy" / "current.env"
    out = {}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def resolve_image(cfg: Config, runner: dict) -> str:
    image = runner["image"]
    if "${AX_TAG}" in image:
        tag = read_current_env(cfg).get("AX_TAG")
        if not tag or not re.match(r"^[A-Za-z0-9_.-]{1,128}$", tag):
            raise internal("image_tag_missing", "current.env に AX_TAG が無い")
        image = image.replace("${AX_TAG}", tag)
    if not re.match(r"^[a-z0-9][a-z0-9._/:@-]{0,255}$", image):
        raise internal("runner_invalid", "image の形が不正")
    return image


def _forbidden_paths(cfg: Config) -> list[Path]:
    """マウント元にしてはいけない場所（F-11.5 の 1）。同じか、中か、含む場合に拒否する。"""
    out = [Path("/"), cfg.home / ".ssh", cfg.home / ".config", cfg.ax_home]
    for getter in (lambda: cfg.secrets_dir, lambda: cfg.state_dir):
        try:
            out.append(getter())
        except LauncherError:
            pass
    return out


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def check_host_path(cfg: Config, raw: str, what: str) -> Path:
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute() or ".." in p.parts:
        raise rejected("bad_mount", f"{what} は ~ を展開した絶対パス（.. なし）")
    try:
        norm = Path(os.path.normpath(str(p)))
        real = Path(os.path.realpath(str(p)))
    except (OSError, ValueError) as e:
        raise rejected("bad_mount", f"{what} を解決できない") from e
    for f in _forbidden_paths(cfg):
        ff = Path(os.path.realpath(str(f)))
        if f == Path("/"):
            hit = norm == f or real == ff  # 「/」は、パスそのものが / のときだけ（含む関係は全部の絶対パスに当たるため）
        else:
            hit = _overlaps(norm, f) or _overlaps(real, ff) or _overlaps(norm, ff) or _overlaps(real, f)
        if hit:
            raise rejected("forbidden_mount", f"{what} が禁止の場所と重なる")
    return norm


def load_ro_registry(cfg: Config) -> dict[str, dict]:
    path = cfg.ax_home / "config" / "ro-mounts.yaml"
    if not path.is_file():
        return {}
    data = _load_yaml(path) or []
    if not isinstance(data, list):
        raise internal("ro_mounts_invalid", "ro-mounts.yaml が配列でない")
    out = {}
    for item in data:
        if not isinstance(item, dict) or not ID_RE.match(str(item.get("id", ""))):
            raise internal("ro_mounts_invalid", "ro-mounts.yaml の項目が不正")
        out[item["id"]] = item
    return out


def resolve_ro_mounts(cfg: Config, ids: list[str]) -> list[tuple[Path, str]]:
    """登録簿の id から (host_path, container_path) を引く。拒否は 2、host_path が無いときは 3（mount_missing）。"""
    if not ids:
        return []
    if len(set(ids)) != len(ids):
        raise rejected("bad_field", "ro_mounts に重複がある")
    reg = load_ro_registry(cfg)
    out: list[tuple[Path, str]] = []
    missing: list[str] = []
    seen_cpaths: list[str] = []
    for i in ids:
        item = reg.get(i)
        if item is None:
            raise rejected("unknown_ro_mount", "登録簿にない id")
        cpath = item.get("container_path")
        if not isinstance(cpath, str) or not cpath.startswith("/") or ".." in Path(cpath).parts:
            raise rejected("bad_mount", "container_path が不正")
        cn = os.path.normpath(cpath)
        for r in RESERVED_CONTAINER_PATHS:
            if cn == r or cn.startswith(r + "/") or r.startswith(cn.rstrip("/") + "/") or cn == "/":
                raise rejected("forbidden_mount", "container_path が /work・/tmp-hdd・/target・/tmp と重なる")
        for other in seen_cpaths:
            if cn == other or cn.startswith(other + "/") or other.startswith(cn + "/"):
                raise rejected("forbidden_mount", "container_path どうしが重なる")
        seen_cpaths.append(cn)
        hp = check_host_path(cfg, str(item.get("host_path", "")), f"host_path({i})")
        if not hp.is_dir():
            missing.append(i)
            continue
        out.append((hp, cn))
    if missing:
        raise LauncherError(EXIT_NOT_FOUND, "not_found", "mount_missing", "host_path が存在するディレクトリでない: " + ", ".join(missing))
    return out


# ---------------------------------------------------------------- Fixed Git input for research
REVISION_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _snapshot_root(cfg: Config) -> Path:
    root = cfg.tmp_dir / "research-snapshots"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise internal("snapshot_root_invalid")
    os.chmod(root, 0o700)
    return root


def _snapshot_manifest(cfg: Config, snapshot_id: str) -> tuple[dict, Path]:
    root = _snapshot_root(cfg) / snapshot_id
    try:
        if root.is_symlink():
            raise ValueError("symlink")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        tree_id = manifest["tree"]
        if not isinstance(tree_id, str) or not re.fullmatch(r"[0-9a-f]{32}", tree_id):
            raise ValueError("invalid tree")
        tree = root / tree_id
        if tree.is_symlink() or not tree.is_dir():
            raise ValueError("missing tree")
        if manifest["snapshot_id"] != snapshot_id:
            raise ValueError("id mismatch")
        return manifest, tree
    except (OSError, ValueError, KeyError, TypeError):
        raise rejected("snapshot_missing_or_invalid") from None


def prepare_snapshot(cfg: Config, snapshot_id: str, text: str) -> dict:
    try:
        request = json.loads(text)
    except (ValueError, TypeError):
        raise rejected("bad_json") from None
    if not isinstance(request, dict) or not {"project", "target", "revision"} <= set(request) or set(request) - {"project", "target", "revision", "attachments"}:
        raise rejected("bad_field")
    request.setdefault("attachments", [])
    attachments = request["attachments"]
    if (not isinstance(attachments, list) or len(attachments) > 3
            or any(not isinstance(x, str) or not ID_RE.fullmatch(x) for x in attachments)
            or len(set(attachments)) != len(attachments)):
        raise rejected("bad_field")
    project, target, revision = (request[k] for k in ("project", "target", "revision"))
    if (not isinstance(project, str) or not project or len(project) > 200
            or not isinstance(target, str) or not ID_RE.fullmatch(target)
            or not isinstance(revision, str) or not REVISION_RE.fullmatch(revision)):
        raise rejected("bad_field")
    root = _snapshot_root(cfg)
    lock = root / ("." + snapshot_id + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        dest = root / snapshot_id
        if dest.is_symlink():
            raise rejected("snapshot_invalid")
        dest.mkdir(mode=0o700, exist_ok=True)
        manifest_path = dest / "manifest.json"
        if manifest_path.exists():
            manifest, _ = _snapshot_manifest(cfg, snapshot_id)
            if any(manifest.get(k) != request[k] for k in request):
                raise rejected("snapshot_mismatch")
            return {k: manifest[k] for k in ("snapshot_id", "project", "target", "revision", "attachments")}
        registry = load_ro_registry(cfg)
        entry = registry.get(target)
        if not entry or entry.get("project") != project:
            raise rejected("unknown_target")
        material_paths = []
        for material_id in attachments:
            material = registry.get(material_id)
            if not material or material.get("project") != project:
                raise rejected("unknown_attachment")
            material_path = check_host_path(cfg, str(material.get("host_path", "")), "attachment")
            if not material_path.is_dir() or material_path.is_symlink():
                raise rejected("attachment_missing")
            files = material.get("files")
            if (not isinstance(files, list) or not files
                    or any(not isinstance(name, str) or not name or name.startswith("/")
                           or any(part in ("", ".", "..") for part in name.split("/")) for name in files)
                    or len(set(files)) != len(files)):
                raise rejected("attachment_files_required")
            material_paths.append((material_id, material_path, files))
        repo = check_host_path(cfg, str(entry.get("host_path", "")), "target")
        if not repo.is_dir():
            raise not_found("target_missing")
        env = {"PATH": os.defpath, "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1",
               "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
        cmd = ["git", "-C", str(repo)]
        def git(*args):
            try:
                return subprocess.run(cmd + list(args), env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, check=True, timeout=120).stdout
            except (OSError, subprocess.SubprocessError):
                raise rejected("revision_unavailable") from None
        if git("rev-parse", "--verify", revision + "^{commit}").strip().decode("ascii") != revision:
            raise rejected("revision_unavailable")
        entries = git("ls-tree", "-r", "-z", "--full-tree", revision).split(b"\0")
        files = []
        for entry_bytes in entries:
            if not entry_bytes:
                continue
            try:
                metadata, raw_path = entry_bytes.split(b"\t", 1)
                mode, kind, oid = metadata.split(b" ")
                path = os.fsdecode(raw_path)
                if (mode not in (b"100644", b"100755") or kind != b"blob" or not raw_path
                        or path.startswith("/") or path.split("/")[0] == ".ax-attachments"
                        or any(part in ("", ".", "..", ".git") for part in path.split("/"))):
                    raise ValueError("unsupported tree")
                files.append((path, oid.decode("ascii")))
            except (ValueError, UnicodeError):
                raise rejected("unsupported_tree") from None
        tree_id = uuid.uuid4().hex
        tree = dest / tree_id
        tree.mkdir(mode=0o700)
        for path, oid in files:
            out = tree.joinpath(*path.split("/"))
            out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with out.open("xb") as stream:
                stream.write(git("cat-file", "blob", oid))
            out.chmod(0o400)
        for material_id, material_path, files in material_paths:
            material_dest = tree / ".ax-attachments" / material_id
            material_dest.mkdir(mode=0o700, parents=True)
            for relative in files:
                parts = relative.split("/")
                parent_fd = None
                try:
                    parent_fd = os.open(material_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    for component in parts[:-1]:
                        next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                        os.close(parent_fd)
                        parent_fd = next_fd
                    source_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                    with os.fdopen(source_fd, "rb") as source:
                        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                            raise rejected("unsupported_attachment")
                        out = material_dest.joinpath(*parts)
                        out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        with out.open("xb") as destination:
                            shutil.copyfileobj(source, destination)
                        out.chmod(0o400)
                except OSError:
                    raise rejected("unsupported_attachment") from None
                finally:
                    if parent_fd is not None:
                        os.close(parent_fd)
        for directory, _, _ in os.walk(tree, topdown=False):
            os.chmod(directory, 0o500)
        manifest = {"snapshot_id": snapshot_id, **request, "tree": tree_id}
        with manifest_path.open("x", encoding="utf-8") as stream:
            os.chmod(manifest_path, 0o600)
            json.dump(manifest, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        return {k: manifest[k] for k in ("snapshot_id", "project", "target", "revision", "attachments")}
    finally:
        os.close(fd)


# ---------------------------------------------------------------- 排他
def valid_verification_host(host: object) -> bool:
    if not isinstance(host, str) or len(host) > 253 or not host or _is_ip_like(host):
        return False
    return all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.'))


def verify_prepare_workspace(cfg: Config, job_id: str, request: dict) -> dict:
    """Prepare a revision-bound repository without inheriting the source Git config."""
    import hashlib
    keys = {'project', 'target', 'revision', 'run_id', 'attempt', 'recipe_id', 'recipe_snapshot_id', 'stage', 'step'}
    if not isinstance(request, dict) or set(request) != keys:
        raise rejected('bad_field')
    for key in ('target', 'run_id', 'attempt', 'recipe_id', 'recipe_snapshot_id'):
        if not isinstance(request[key], str) or not ID_RE.fullmatch(request[key]):
            raise rejected('bad_field')
    if (not isinstance(request['project'], str) or not 0 < len(request['project']) <= 200
            or not isinstance(request['revision'], str) or not REVISION_RE.fullmatch(request['revision'])
            or request['stage'] not in ('dependency', 'verify')
            or not isinstance(request['step'], str) or not ID_RE.fullmatch(request['step'])):
        raise rejected('bad_field')
    registry = load_ro_registry(cfg)
    target = registry.get(request['target'])
    if not target or target.get('project') != request['project']:
        raise rejected('unknown_target')
    recipe_file = cfg.ax_home / 'config' / 'verification-recipes.yaml'
    recipes = _load_yaml(recipe_file)
    recipe = recipes.get(request['recipe_id']) if isinstance(recipes, dict) else None
    if (not isinstance(recipe, dict) or recipe.get('project') != request['project']
            or recipe.get('target') != request['target']):
        raise rejected('unknown_recipe')
    snapshot_path = cfg.state_dir / 'verification-recipes' / (request['recipe_snapshot_id'] + '.json')
    try:
        fd_snapshot = os.open(snapshot_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd_snapshot, encoding='utf-8') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError('snapshot not regular')
            snapshot = json.load(source)
    except (OSError, ValueError, TypeError):
        raise rejected('recipe_snapshot_invalid') from None
    if snapshot != {'project': request['project'], 'target': request['target'],
                    'recipe_id': request['recipe_id'], 'recipe': recipe}:
        raise rejected('recipe_snapshot_mismatch')
    recipe = snapshot['recipe']
    dependencies = recipe.get('dependency', [])
    checks = recipe.get('checks', [])
    if not isinstance(dependencies, list) or not isinstance(checks, list):
        raise rejected('invalid_recipe')
    commands = {}
    for index, command_value in enumerate(dependencies):
        if not isinstance(command_value, str) or not command_value:
            raise rejected('invalid_recipe')
        commands[('dependency', 'dependency-' + str(index + 1))] = command_value
    for check in [*checks, *[recipe[k] for k in ('all', 'slow') if k in recipe]]:
        if (not isinstance(check, dict) or not isinstance(check.get('kind'), str)
                or not ID_RE.fullmatch(check['kind']) or not isinstance(check.get('command'), str)
                or not check['command']):
            raise rejected('invalid_recipe')
        key = ('verify', check['kind'])
        if key in commands and commands[key] != check['command']:
            raise rejected('invalid_recipe')
        commands[key] = check['command']
    command = commands.get((request['stage'], request['step']))
    if command is None:
        raise rejected('unknown_step')
    if (not isinstance(recipe.get('version'), str) or not recipe['version']
            or not isinstance(recipe.get('runner'), str) or not ID_RE.fullmatch(recipe['runner'])
            or type(recipe.get('timeout_s')) is not int or not 1 <= recipe['timeout_s'] <= 2400):
        raise rejected('invalid_recipe')
    repo = check_host_path(cfg, str(target.get('host_path', '')), 'target')
    if not repo.is_dir():
        raise not_found('target_missing')
    root = cfg.state_dir / 'verification'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    scope = {key: request[key] for key in ('project', 'target', 'recipe_id', 'recipe_snapshot_id')}
    scope['recipe_snapshot'] = recipe
    run_dir = root / request['run_id']
    run_dir.mkdir(mode=0o700, exist_ok=True)
    lock_fd = os.open(run_dir / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        scope_path = run_dir / 'scope.json'
        if scope_path.exists():
            if json.loads(scope_path.read_text()) != scope:
                raise rejected('run_scope_mismatch')
        else:
            _atomic_write(scope_path, json.dumps(scope, sort_keys=True), 0o600)
        key = json.dumps([request['run_id'], request['revision'], request['attempt']], separators=(',', ':')).encode()
        wd = 'verify-' + hashlib.sha256(key).hexdigest()[:32]
        work = cfg.work_of(wd)
        manifest = run_dir / (wd + '.json')
        identity = {key: request[key] for key in ('project', 'target', 'recipe_id')}
        identity.update({'revision': request['revision'], 'attempt': request['attempt'], 'workdir_id': wd})
        if manifest.exists():
            if json.loads(manifest.read_text()) != identity or not work.is_dir():
                raise rejected('workdir_mismatch')
        else:
            if work.exists():
                raise rejected('workdir_conflict')
            work.mkdir(mode=0o700, parents=True)
            _atomic_write(manifest, json.dumps(identity, sort_keys=True), 0o600)
        input_root = cfg.work_dir / 'verification-inputs'
        input_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        input_dir = input_root / job_id
        input_manifest = run_dir / ('input-' + job_id + '.json')
        input_identity = {**identity, 'job_id': job_id}
        dest = input_dir / 'repo'
        if input_dir.exists():
            if (not input_manifest.is_file() or json.loads(input_manifest.read_text()) != input_identity
                    or not (dest / '.git').is_dir()):
                raise rejected('input_conflict')
        else:
            input_dir.mkdir(mode=0o700)
            env = {'PATH': os.defpath, 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_NO_REPLACE_OBJECTS': '1',
                   'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'}
            def git(args, cwd=None):
                try:
                    return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=cwd,
                        env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=120).stdout
                except (OSError, subprocess.SubprocessError):
                    raise rejected('revision_unavailable') from None
            actual = git(['-C', str(repo), 'rev-parse', '--verify', request['revision'] + '^{commit}']).strip().decode('ascii')
            if actual != request['revision']:
                raise rejected('revision_unavailable')
            object_format = git(['-C', str(repo), 'rev-parse', '--show-object-format']).strip().decode('ascii')
            if object_format not in ('sha1', 'sha256') or len(request['revision']) != (40 if object_format == 'sha1' else 64):
                raise rejected('revision_unavailable')
            git(['init', '-q', '--template=', '--object-format=' + object_format, str(dest)])
            git(['-c', 'protocol.file.allow=always', 'fetch', '--no-tags', '--no-write-fetch-head',
                 '--no-recurse-submodules', str(repo), request['revision']], cwd=dest)
            git(['checkout', '--detach', '--force', request['revision']], cwd=dest)
            if git(['rev-parse', 'HEAD'], cwd=dest).strip().decode('ascii') != request['revision']:
                raise rejected('revision_unavailable')
            _atomic_write(input_manifest, json.dumps(input_identity, sort_keys=True), 0o600)
        return {**identity, 'job_id': job_id, 'repo_path': str(dest), 'recipe_version': recipe['version'],
                'runner': recipe['runner'], 'command': command, 'timeout_s': recipe['timeout_s'], '_recipe': recipe}
    finally:
        os.close(lock_fd)


class Locks:
    def __init__(self):
        self._fds: list[int] = []

    def take(self, path: Path, reason: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise busy(reason, "同じ場所か会話の連鎖を別のジョブが使っている") from None
        self._fds.append(fd)

    def release(self) -> None:
        for fd in self._fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._fds = []


# ---------------------------------------------------------------- コマンド行の組み立て
def render_command(template: str, values: dict[str, str]) -> list[str]:
    """runner の command を shlex で分け、各語の {名前} を置き換える。値は語の中にだけ入り、語を増やさない。"""
    words = shlex.split(template)
    out = []
    for w in words:
        for k, v in values.items():
            w = w.replace("{" + k + "}", v)
        out.append(w)
    return out


def render_env(env: dict, values: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in env.items():
        s = str(v)
        for name, val in values.items():
            s = s.replace("{" + name + "}", val)
        out[k] = s
    return out


def build_run_args(cfg: Config, req: dict, runner: dict, *, image: str, root: str, wd: str, session_arg: str,
                   ro_mounts: list[tuple[Path, str]], target_ro: Path | None, extra_env_names: list[str]) -> list[str]:
    job_id = req["job_id"]
    values = {"model": req.get("model", ""), "job_id": job_id, "session_arg": session_arg, "verify": req.get("verify", "")}
    uid, gid = os.getuid(), os.getgid()
    mem = runner.get("memory", "4g")
    cpus = str(runner.get("cpus", "2"))
    pids = str(runner.get("pids_limit", 512))
    proxy_url = f"http://job-{job_id}@{PROXY_HOST}:{PROXY_PORT}"
    args = ["run", "-d", "--name", f"ax-job-{job_id}",
            "--label", "ax.managed=1", "--label", f"ax.job={job_id}", "--label", f"ax.chain={root}", "--label", f"ax.wd={wd}",
            "--user", f"{uid}:{gid}", "--read-only", "--tmpfs", "/tmp:rw,size=512m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", pids, "--memory", mem, "--cpus", cpus,
            "--network", cfg.network,
            "-v", f"{cfg.work_of(wd)}:/work:rw", "-w", "/work",
            "-v", f"{cfg.chain_of(root)}:/tmp-hdd:rw", "-e", "TMPDIR=/tmp-hdd", "-e", "HOME=/tmp-hdd/home",
            "-e", f"HTTP_PROXY={proxy_url}", "-e", f"HTTPS_PROXY={proxy_url}", "-e", f"NO_PROXY={PROXY_HOST}"]
    if runner["kind"] in KINDS_WITH_MODEL:
        args += ["-e", f"ANTHROPIC_BASE_URL=http://{PROXY_HOST}:{GATEWAY_PORT}", "-e", "ANTHROPIC_AUTH_TOKEN"]
    if target_ro is not None:
        args += ["-v", f"{target_ro}:/target:ro"]
    for hp, cp in ro_mounts:
        args += ["-v", f"{hp}:{cp}:ro"]
    for k, v in render_env(runner.get("env") or {}, values).items():
        args += ["-e", f"{k}={v}"]
    for name in extra_env_names:
        args += ["-e", name]
    if runner["kind"] == "chat-json":
        args += ["-e", "PYTHONPATH=/ax", "-e", "PYTHONNOUSERSITE=1"]
    args.append(image)
    args += render_command(runner["command"], values)
    return args


# ---------------------------------------------------------------- 起動後の確認
def check_isolation(info: dict, cfg: Config, *, expect_mounts: set[str]) -> list[str]:
    """docker inspect の結果が F-18.2 の隔離になっているか。ならなければ理由の一覧を返す。"""
    problems = []
    hc = info.get("HostConfig") or {}
    if hc.get("Privileged"):
        problems.append("Privileged")
    if hc.get("CapAdd"):
        problems.append("CapAdd")
    if not hc.get("ReadonlyRootfs"):
        problems.append("ReadonlyRootfs")
    if hc.get("NetworkMode") != cfg.network:
        problems.append("NetworkMode")
    mounts = info.get("Mounts") or []
    got = {m.get("Destination") for m in mounts}
    if got != expect_mounts:
        problems.append("Mounts")
    for m in mounts:
        if "docker.sock" in str(m.get("Source", "")) or str(m.get("Type")) not in ("bind", "volume", "tmpfs"):
            problems.append("socket_or_type")
    for b in hc.get("Binds") or []:
        if "docker.sock" in b:
            problems.append("socket_bind")
    return problems


def verify_inspection_settings(info: dict) -> dict:
    """Capture only non-secret settings required to remain fixed until exit."""
    config = info.get('Config') or {}
    host = info.get('HostConfig') or {}
    return {
        'config': {k: config.get(k) for k in ('Image', 'Cmd', 'User', 'WorkingDir', 'Entrypoint')},
        'environment_names': sorted(x.split('=', 1)[0] for x in (config.get('Env') or []) if isinstance(x, str) and '=' in x),
        'fixed_environment': sorted(x for x in (config.get('Env') or []) if isinstance(x, str) and x.split('=', 1)[0] in {'HOME', 'TMPDIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'EGRESS_FIXED_ALLOW_FILE'}),
        'host': {k: host.get(k) for k in ('Privileged', 'CapAdd', 'ReadonlyRootfs', 'NetworkMode', 'CapDrop', 'SecurityOpt', 'LogConfig', 'Binds', 'Dns', 'Sysctls')},
        'mounts': sorted([str(m.get('Destination')), str(m.get('Source')), m.get('RW')] for m in (info.get('Mounts') or [])),
    }


def check_chat_isolation(info: dict, cfg: Config, image: str, wd: str, root: str) -> list[str]:
    """Inspect the running container against the trusted host-managed chat profile."""
    problems = []
    config = info.get("Config") or {}
    hc = info.get("HostConfig") or {}
    if config.get("Image") != image or config.get("Cmd") != ["python", "-P", "-m", "axcommon.chat_json"]:
        problems.append("chat_command_or_image")
    if config.get("Entrypoint") not in (None, []):
        problems.append("chat_entrypoint")
    if config.get("WorkingDir") != "/work" or config.get("User") != f"{os.getuid()}:{os.getgid()}":
        problems.append("chat_workdir_or_user")
    env = config.get("Env")
    if not isinstance(env, list) or any(not isinstance(x, str) or "=" not in x for x in env):
        problems.append("chat_environment")
    else:
        pairs = [x.split("=", 1) for x in env]
        python_env = [(k, v) for k, v in pairs if k.startswith("PYTHON")]
        if (len([1 for k, _ in python_env if k == "PYTHONPATH"]) != 1
                or ("PYTHONPATH", "/ax") not in python_env
                or len([1 for k, _ in python_env if k == "PYTHONNOUSERSITE"]) != 1
                or ("PYTHONNOUSERSITE", "1") not in python_env
                or any(k not in {"PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PYTHON_VERSION", "PYTHON_SHA256", "PYTHON_PIP_VERSION", "PYTHON_SETUPTOOLS_VERSION", "PYTHON_GET_PIP_URL", "PYTHON_GET_PIP_SHA256"} for k, _ in python_env)):
            problems.append("chat_python_environment")
    mounts = info.get("Mounts") or []
    expected = {"/work": str(cfg.work_of(wd)), "/tmp-hdd": str(cfg.chain_of(root))}
    if len(mounts) != 2 or any(m.get("Destination") not in expected or m.get("Source") != expected.get(m.get("Destination")) or m.get("RW") is not True or m.get("Type") != "bind" for m in mounts):
        problems.append("chat_mounts")
    if "ALL" not in (hc.get("CapDrop") or []) or not any(str(x).split("=", 1)[0] == "no-new-privileges" for x in (hc.get("SecurityOpt") or [])):
        problems.append("chat_capabilities")
    return problems


# ---------------------------------------------------------------- 使用量
def extract_usage(jsonl: Path) -> dict:
    """会話の記録から first_input / first_cache_read / last_context を取る（F-18.2 の終了時の使用量）。取れた項目だけ返す。"""
    first = last = None
    try:
        with jsonl.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "assistant" or rec.get("isSidechain"):
                    continue
                usage = (rec.get("message") or {}).get("usage")
                if not isinstance(usage, dict):
                    continue
                if first is None:
                    first = usage
                last = usage
    except OSError:
        return {}
    out: dict = {}
    if first is not None:
        if isinstance(first.get("input_tokens"), int):
            out["first_input"] = first["input_tokens"]
        if isinstance(first.get("cache_read_input_tokens"), int):
            out["first_cache_read"] = first["cache_read_input_tokens"]
    if last is not None:
        parts = [last.get(k) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
        if all(isinstance(p, int) for p in parts):
            out["last_context"] = sum(parts)
    return out


# ---------------------------------------------------------------- 本体
class Launcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.docker = Docker(cfg)

    def _verify_path(self, job_id):
        return self.cfg.state_dir / 'verification-jobs' / (job_id + '.json')

    def _verify_load(self, job_id):
        try:
            data = json.loads(self._verify_path(job_id).read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('job_id') != job_id:
                raise ValueError('bad record')
            return data
        except (OSError, ValueError):
            raise not_found('verification_record_missing') from None

    def _verify_save(self, job_id, record):
        _atomic_write(self._verify_path(job_id), json.dumps(record, sort_keys=True) + '\n', 0o600)

    def _verify_lock(self, job_id):
        path = self._verify_path(job_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path.parent / (job_id + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _verify_timer(self, job_id, seconds):
        cfg = self.cfg
        runtime = '/run/user/' + str(os.getuid())
        env = dict(cfg.env)
        env['XDG_RUNTIME_DIR'] = runtime
        env['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=' + runtime + '/bus'
        unit = 'ax-verify-' + job_id
        args = ['systemd-run', '--user', '--quiet', '--unit=' + unit,
                '--on-active=' + str(seconds) + 's', '--timer-property=AccuracySec=1us']
        timer_env = {'AX_HOME': str(cfg.ax_home), 'AX_ENV_HOME': str(cfg.env_home),
                     'AX_PROFILE': str(cfg.profile_path), 'AX_WORK_DIR': str(cfg.work_dir)}
        for key in ('AX_ENV_SUDO', 'AX_ENV_DOCKER'):
            if key in cfg.env:
                timer_env[key] = cfg.env[key]
        for key, val in timer_env.items():
            args.append('--setenv=' + key + '=' + val)
        args += [sys.executable, '-P', str(Path(__file__).resolve()), '--caller', 'launcher', 'job-verify-expire', job_id]
        try:
            launched = subprocess.run(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if launched.returncode:
                raise internal('verification_timer_unavailable')
            check = subprocess.run(['systemctl', '--user', 'is-active', unit + '.timer'], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15)
            if check.returncode or check.stdout.strip() != 'active':
                raise internal('verification_timer_unavailable')
        except (OSError, subprocess.SubprocessError):
            raise internal('verification_timer_unavailable') from None

    def job_verify_recipe(self, request_id, text):
        try:
            request = json.loads(text)
        except (TypeError, ValueError):
            raise rejected('bad_json') from None
        if (not isinstance(request, dict) or set(request) != {'project', 'target', 'recipe_id'}
                or not isinstance(request['project'], str) or not 0 < len(request['project']) <= 200
                or any(not isinstance(request[k], str) or not ID_RE.fullmatch(request[k]) for k in ('target', 'recipe_id'))):
            raise rejected('bad_field')
        directory = self.cfg.state_dir / 'verification-recipes'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = os.open(directory / ('request-' + request_id + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            request_path = directory / ('request-' + request_id + '.json')
            if request_path.exists():
                try:
                    fd = os.open(request_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, encoding='utf-8') as source:
                        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                            raise ValueError('invalid record')
                        existing = json.load(source)
                    if any(existing.get(k) != v for k, v in request.items()):
                        raise rejected('recipe_request_mismatch')
                    return existing
                except (OSError, ValueError, TypeError):
                    raise rejected('recipe_request_invalid') from None
            target = load_ro_registry(self.cfg).get(request['target'])
            if not target or target.get('project') != request['project']:
                raise rejected('unknown_target')
            recipes = _load_yaml(self.cfg.ax_home / 'config' / 'verification-recipes.yaml')
            recipe = recipes.get(request['recipe_id']) if isinstance(recipes, dict) else None
            if not isinstance(recipe, dict) or recipe.get('project') != request['project'] or recipe.get('target') != request['target']:
                raise rejected('unknown_recipe')
            allow = recipe.get('outbound_allowlist', [])
            if (not isinstance(recipe.get('version'), str) or not recipe['version'] or not isinstance(allow, list)
                    or len(allow) > 64 or any(not valid_verification_host(h) for h in allow)):
                raise rejected('invalid_recipe')
            snapshot_id = 'vs-' + uuid.uuid4().hex
            snapshot = {**request, 'recipe': recipe}
            fd = os.open(directory / (snapshot_id + '.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(snapshot, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            response = {**request, 'request_id': request_id, 'recipe_snapshot_id': snapshot_id,
                        'recipe_version': recipe['version'], 'allow_hosts': allow,
                        'prestart_isolation_enforced': True}
            _atomic_write(directory / ('request-' + request_id + '.json'), json.dumps(response, sort_keys=True), 0o600)
            return response

        finally:
            os.close(lock)

    def _verify_network_inspect(self, name):
        result = self.docker.run(['network', 'inspect', name])
        if result.returncode:
            raise internal('verification_network_unavailable')
        try:
            data = json.loads(result.stdout)
            if not isinstance(data, list) or len(data) != 1 or data[0].get('Name') != name:
                raise ValueError('invalid network inspect')
            return data[0]
        except (TypeError, ValueError):
            raise internal('verification_network_unavailable') from None

    def _verify_stop_proxy(self, job_id):
        name = 'ax-vp-' + job_id
        info = self.docker.inspect(name)
        if info is not None and (info.get('State') or {}).get('Status') not in ('exited', 'dead', 'created'):
            stopped = self.docker.run(['stop', '-t', '0', name], timeout=30)
            if stopped.returncode:
                raise internal('verification_proxy_stop_failed')

    def _verify_start_proxy(self, job_id, recipe):
        allow = recipe.get('outbound_allowlist', [])
        if not isinstance(allow, list) or not allow or len(allow) > 64 or any(not valid_verification_host(x) for x in allow):
            raise rejected('dependency_allowlist_missing')
        internal_net, outer_net, name = 'ax-vj-' + job_id, 'ax-vo-' + job_id, 'ax-vp-' + job_id
        policy_dir = self.cfg.state_dir / 'verification-proxy'
        policy_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        policy = policy_dir / (job_id + '.allow')
        fd = os.open(policy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write('\n'.join(dict.fromkeys(allow)) + '\n')
        try:
            routes = subprocess.run(['ip', '-j', '-4', 'route', 'show', 'table', 'all'],
                                    capture_output=True, text=True, check=True, timeout=15)
            occupied_routes = [ipaddress.ip_network(row['dst'], strict=False) for row in json.loads(routes.stdout)
                               if isinstance(row, dict) and row.get('dst') not in (None, 'default')]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            raise internal('verification_network_unavailable') from None
        for name_to_create, octet, internal in ((internal_net, 250, True), (outer_net, 251, False)):
            created_network = False
            for _ in range(12):
                number = uuid.uuid4().int % 4096
                subnet = f'10.{octet}.{number // 16}.{(number % 16) * 16}/28'
                if any(ipaddress.ip_network(subnet).overlaps(route) for route in occupied_routes):
                    continue
                args = ['network', 'create', '--subnet', subnet] + (['--internal'] if internal else []) + [name_to_create]
                if self.docker.run(args, timeout=60).returncode == 0:
                    created_network = True
                    break
            if not created_network:
                raise internal('verification_network_unavailable')
        image = resolve_image(self.cfg, {'image': 'ax/core:${AX_TAG}'})
        args = ['create', '--name', name, '--network', internal_net, '--network-alias', 'verify-proxy',
                '--user', f'{os.getuid()}:{os.getgid()}', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--sysctl', 'net.ipv4.ip_forward=0',
                '--sysctl', 'net.ipv6.conf.all.forwarding=0', '-v', str(policy) + ':/policy/allow:ro',
                '-e', 'EGRESS_FIXED_ALLOW_FILE=/policy/allow', '--entrypoint', 'python', image,
                '/ax/core/egress-proxy/egress_proxy.py']
        if self.docker.run(args, timeout=180).returncode:
            raise internal('verification_proxy_unavailable')
        created = self.docker.inspect(name)
        cconfig, chc = (created or {}).get('Config') or {}, (created or {}).get('HostConfig') or {}
        cmounts = (created or {}).get('Mounts') or []
        if (created is None or cconfig.get('Image') != image or cconfig.get('User') != f'{os.getuid()}:{os.getgid()}'
                or cconfig.get('Entrypoint') != ['python'] or cconfig.get('Cmd') != ['/ax/core/egress-proxy/egress_proxy.py']
                or [x for x in (cconfig.get('Env') or []) if x.startswith('EGRESS_')] != ['EGRESS_FIXED_ALLOW_FILE=/policy/allow']
                or not chc.get('ReadonlyRootfs') or chc.get('Privileged') or chc.get('CapAdd')
                or 'ALL' not in (chc.get('CapDrop') or []) or 'no-new-privileges' not in (chc.get('SecurityOpt') or [])
                or (chc.get('Sysctls') or {}).get('net.ipv4.ip_forward') != '0'
                or (chc.get('Sysctls') or {}).get('net.ipv6.conf.all.forwarding') != '0'
                or chc.get('NetworkMode') != internal_net or chc.get('PortBindings') or len(cmounts) != 1
                or cmounts[0].get('Source') != str(policy) or cmounts[0].get('Destination') != '/policy/allow'
                or cmounts[0].get('RW') is not False):
            raise internal('verification_proxy_unconfirmed')
        if self.docker.run(['start', name], timeout=60).returncode:
            raise internal('verification_proxy_unavailable')
        if self.docker.run(['network', 'connect', outer_net, name], timeout=60).returncode:
            self._verify_stop_proxy(job_id)
            raise internal('verification_network_unavailable')
        info = self.docker.inspect(name)
        config, hc = (info or {}).get('Config') or {}, (info or {}).get('HostConfig') or {}
        mounts = (info or {}).get('Mounts') or []
        nets = ((info or {}).get('NetworkSettings') or {}).get('Networks') or {}
        if (info is None or config.get('Image') != image or config.get('User') != f'{os.getuid()}:{os.getgid()}'
                or config.get('Entrypoint') != ['python'] or config.get('Cmd') != ['/ax/core/egress-proxy/egress_proxy.py']
                or [x for x in (config.get('Env') or []) if x.startswith('EGRESS_')] != ['EGRESS_FIXED_ALLOW_FILE=/policy/allow']
                or not hc.get('ReadonlyRootfs') or hc.get('Privileged') or hc.get('CapAdd')
                or 'ALL' not in (hc.get('CapDrop') or []) or 'no-new-privileges' not in (hc.get('SecurityOpt') or [])
                or (hc.get('Sysctls') or {}).get('net.ipv4.ip_forward') != '0'
                or (hc.get('Sysctls') or {}).get('net.ipv6.conf.all.forwarding') != '0'
                or hc.get('PortBindings') or len(mounts) != 1 or mounts[0].get('Source') != str(policy)
                or mounts[0].get('Destination') != '/policy/allow' or mounts[0].get('RW') is not False
                or set(nets) != {internal_net, outer_net} or 'verify-proxy' not in (nets[internal_net].get('Aliases') or [])):
            raise internal('verification_proxy_unconfirmed')
        for sysctl in ('ipv4/ip_forward', 'ipv6/conf/all/forwarding'):
            result = self.docker.run(['exec', name, 'cat', '/proc/sys/net/' + sysctl], timeout=30)
            if result.returncode or result.stdout.strip() != '0':
                self._verify_stop_proxy(job_id)
                raise internal('verification_proxy_unconfirmed')
        network = self._verify_network_inspect(internal_net)
        other = self._verify_network_inspect(outer_net)
        if network.get('Internal') is not True or other.get('Internal') is not False:
            self._verify_stop_proxy(job_id)
            raise internal('verification_network_unconfirmed')
        for item in (network, other):
            members = [v.get('Name') for v in (item.get('Containers') or {}).values()]
            if members != [name]:
                self._verify_stop_proxy(job_id)
                raise internal('verification_network_unconfirmed')
        return internal_net

    def _verify_proxy_still_safe(self, job_id, settings):
        name, internal_net, outer_net = 'ax-vp-' + job_id, 'ax-vj-' + job_id, 'ax-vo-' + job_id
        info = self.docker.inspect(name)
        if info is None or (info.get('State') or {}).get('Status') != 'running' or verify_inspection_settings(info) != settings:
            return False
        networks = ((info.get('NetworkSettings') or {}).get('Networks') or {})
        if set(networks) != {internal_net, outer_net} or 'verify-proxy' not in (networks[internal_net].get('Aliases') or []):
            return False
        try:
            inner, outer = self._verify_network_inspect(internal_net), self._verify_network_inspect(outer_net)
            if inner.get('Internal') is not True or outer.get('Internal') is not False:
                return False
            inner_members = {v.get('Name') for v in (inner.get('Containers') or {}).values()}
            outer_members = {v.get('Name') for v in (outer.get('Containers') or {}).values()}
            if name not in inner_members or inner_members - {name, 'ax-verify-' + job_id} or outer_members != {name}:
                return False
            for sysctl in ('ipv4/ip_forward', 'ipv6/conf/all/forwarding'):
                result = self.docker.run(['exec', name, 'cat', '/proc/sys/net/' + sysctl], timeout=30)
                if result.returncode or result.stdout.strip() != '0':
                    return False
            return True
        except LauncherError:
            return False

    def _verify_artifact_mounts(self, request, prepared, recipe):
        names = recipe.get('dependency_artifacts', [])
        if (not isinstance(names, list) or len(names) > 32
                or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}', name)
                       or name in ('.git', '.ax-prompt') for name in names) or len(set(names)) != len(names)):
            raise rejected('invalid_dependency_artifacts')
        repo = Path(prepared['repo_path'])
        env = {'PATH': os.defpath, 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'}
        try:
            result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'ls-files', '-z'], cwd=repo,
                                    env=env, capture_output=True, check=True, timeout=60)
            tracked = result.stdout.split(b'\0')
        except (OSError, subprocess.SubprocessError):
            raise rejected('artifact_validation_failed') from None
        work = self.cfg.work_of(prepared['workdir_id'])
        artifacts = work / 'artifacts'
        if artifacts.is_symlink():
            raise rejected('artifact_validation_failed')
        artifacts.mkdir(mode=0o700, exist_ok=True)
        mounts = []
        for name in names:
            prefix = os.fsencode(name)
            if any(t == prefix or t.startswith(prefix + b'/') for t in tracked):
                raise rejected('artifact_overlaps_source')
            host = artifacts / name
            target = repo / name
            if host.is_symlink() or target.is_symlink():
                raise rejected('artifact_validation_failed')
            host.mkdir(mode=0o700, exist_ok=True)
            target.mkdir(mode=0o700, exist_ok=True)
            mounts.append((host, '/work/repo/' + name))
        return artifacts, mounts

    def _verify_require_dependencies(self, request, prepared, recipe):
        dependencies = recipe.get('dependency', [])
        if request['stage'] == 'dependency':
            match = re.fullmatch(r'dependency-([1-9][0-9]*)', request['step'])
            if not match:
                raise rejected('unknown_step')
            count = int(match.group(1)) - 1
            own_marker = self.cfg.state_dir / 'verification' / request['run_id'] / (prepared['workdir_id'] + '-' + request['step'] + '.json')
            if own_marker.exists():
                raise rejected('dependency_already_completed')
        else:
            count = len(dependencies)
        directory = self.cfg.state_dir / 'verification' / request['run_id']
        for index in range(1, count + 1):
            marker = directory / (prepared['workdir_id'] + '-dependency-' + str(index) + '.json')
            try:
                fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, encoding='utf-8') as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError('invalid marker')
                    previous = json.load(source)
                if any(previous.get(k) != request[k] for k in ('project', 'target', 'revision', 'run_id', 'attempt', 'recipe_id', 'recipe_snapshot_id')):
                    raise ValueError('different attempt')
                prior = self.job_verify_result(previous['job_id'])
                if (prior.get('state') != 'exited' or prior.get('exit_code') != 0 or prior.get('oom_killed') is not False
                        or prior.get('timed_out') is not False or prior.get('isolation_verified') is not True
                        or prior.get('log_complete') is not True or prior.get('step') != 'dependency-' + str(index)
                        or any(prior.get(k) != request[k] for k in ('project', 'target', 'revision', 'run_id', 'attempt', 'recipe_id', 'recipe_snapshot_id'))):
                    raise ValueError('dependency evidence incomplete')
            except (OSError, KeyError, ValueError, TypeError, LauncherError):
                raise rejected('dependency_not_completed') from None

    def job_verify_prepare(self, job_id, text):
        try:
            request = json.loads(text)
        except (TypeError, ValueError):
            raise rejected('bad_json') from None
        prepared = verify_prepare_workspace(self.cfg, job_id, request)
        recipe = prepared['_recipe']
        self._verify_require_dependencies(request, prepared, recipe)
        artifacts, artifact_mounts = self._verify_artifact_mounts(request, prepared, recipe) if recipe.get('dependency') else (None, [])
        runner = load_runner(self.cfg, prepared['runner'])
        if runner.get('kind') != 'verify' or '{verify}' not in runner.get('command', ''):
            raise rejected('invalid_verify_runner')
        permitted_runner_env = {'CI', 'AX_CI_IN_JOB', 'NUGET_PACKAGES', 'DOTNET_CLI_TELEMETRY_OPTOUT',
                                'DOTNET_NOLOGO', 'DOTNET', 'DOTNET_ROLL_FORWARD', 'GameDir', 'ModDeployDir',
                                'UV_CACHE_DIR', 'PIP_CACHE_DIR', 'npm_config_cache', 'PNPM_HOME'}
        if set((runner.get('env') or {}).keys()) - permitted_runner_env:
            raise rejected('verification_environment_forbidden')
        image = resolve_image(self.cfg, runner)
        if recipe.get('ro_mounts'):
            raise rejected('verification_mounts_forbidden')
        mounts = []
        cfg = self.cfg
        root = job_id
        wd = prepared['workdir_id']
        chain = cfg.chain_of(root)
        try:
            chain.mkdir(mode=0o700, parents=True, exist_ok=False)
            (chain / 'home').mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            raise rejected('verification_chain_conflict') from None
        fd = self._verify_lock(job_id)
        try:
            if self._verify_path(job_id).exists():
                raise rejected('job_id_in_use')
            record = {**request, **{k: v for k, v in prepared.items() if k not in ('repo_path', '_recipe')}, 'stage': request['stage'], 'step': request['step'],
                      'state': 'starting', 'network_mode': 'allowlist' if request['stage'] == 'dependency' else 'none',
                      'allow_hosts': recipe.get('outbound_allowlist', []) if request['stage'] == 'dependency' else [],
                      'isolation_verified': False, 'home_absent': False, 'credentials_absent': False,
                      'host_config_absent': False, 'network_enforced': False, 'started_at': None, 'ended_at': None,
                      'exit_code': None, 'oom_killed': None, 'timed_out': False,
                      'executed': False, 'log_complete': False, 'output_truncated': True,
                      'deadline_at': (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=prepared['timeout_s'])).isoformat()}
            self._verify_save(job_id, record)
        finally:
            os.close(fd)
        try:
            self._verify_timer(job_id, prepared['timeout_s'])
        except Exception:
            fd = self._verify_lock(job_id)
            try:
                record = self._verify_load(job_id)
                record['state'] = 'unknown'
                self._verify_save(job_id, record)
            finally:
                os.close(fd)
            raise
        fd = self._verify_lock(job_id)
        try:
            record = self._verify_load(job_id)
            if record['timed_out'] or record['state'] != 'starting':
                raise internal('verification_timed_out')
        finally:
            os.close(fd)
        cfg.jobs_dir.mkdir(parents=True, exist_ok=True)
        work_lock = os.open(cfg.jobs_dir / ('.wd-' + wd + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(work_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise busy('workdir_busy') from None
            if self.docker.ps_ids('label=ax.wd=' + wd):
                raise busy('workdir_busy')
            if request['stage'] == 'dependency':
                try:
                    dep_network = self._verify_start_proxy(job_id, recipe)
                except Exception:
                    self._verify_stop_proxy(job_id)
                    raise
            else:
                dep_network = None
            req = {'job_id': job_id, 'runner': prepared['runner'], 'verify': 'cd /work/repo && ' + prepared['command'],
                   'allow_hosts': [], 'budget_min': max(1, (prepared['timeout_s'] + 59) // 60), 'prompt': 'Verification'}
            args = build_run_args(cfg, req, runner, image=image, root=root, wd=wd, session_arg='',
                                  ro_mounts=mounts, target_ro=None, extra_env_names=[])
            mount_point = cfg.work_of(wd) / 'repo'
            if mount_point.is_symlink() or (mount_point.exists() and not mount_point.is_dir()):
                raise rejected('workdir_mismatch')
            mount_point.mkdir(mode=0o700, exist_ok=True)
            args[args.index(image):args.index(image)] = ['-v', prepared['repo_path'] + ':/work/repo:rw']
            args[args.index('--network') + 1] = dep_network or 'none'
            if dep_network:
                args[args.index(image):args.index(image)] = ['--dns', '127.0.0.1']
                for old, new in ((f'HTTP_PROXY=http://job-{job_id}@{PROXY_HOST}:{PROXY_PORT}', 'HTTP_PROXY=http://verify-proxy:3128'),
                                 (f'HTTPS_PROXY=http://job-{job_id}@{PROXY_HOST}:{PROXY_PORT}', 'HTTPS_PROXY=http://verify-proxy:3128'),
                                 (f'NO_PROXY={PROXY_HOST}', 'NO_PROXY=verify-proxy')):
                    args[args.index(old)] = new
            if artifacts is not None:
                if request['stage'] == 'verify':
                    args[args.index(image):args.index(image)] = ['-v', str(artifacts) + ':/work/artifacts:ro']
                mode = 'rw' if request['stage'] == 'dependency' else 'ro'
                for source, destination in artifact_mounts:
                    args[args.index(image):args.index(image)] = ['-v', str(source) + ':' + destination + ':' + mode]
            args[args.index('--name') + 1] = 'ax-verify-' + job_id
            args[2:2] = ['--log-driver', 'json-file', '--log-opt', 'mode=blocking']
            args = ['create'] + args[2:]
            result = self.docker.run(args, timeout=180)
            if result.returncode:
                raise internal('docker_run_failed')
            info = self.docker.inspect('ax-verify-' + job_id)
            expected = {'/work', '/work/repo', '/tmp-hdd'} | {cp for _, cp in mounts} | {cp for _, cp in artifact_mounts}
            if artifacts is not None and request['stage'] == 'verify':
                expected.add('/work/artifacts')
            problems = ['inspect_failed'] if info is None else check_isolation(info, cfg, expect_mounts=expected)
            if info is not None:
                problems = [p for p in problems if p != 'NetworkMode']
                host = info.get('HostConfig') or {}
                actual_mounts = {m.get('Destination'): (m.get('Source'), m.get('RW')) for m in info.get('Mounts', [])}
                expected_mounts = {'/work': (str(cfg.work_of(wd)), True), '/work/repo': (prepared['repo_path'], True), '/tmp-hdd': (str(chain), True)}
                expected_mounts.update({cp: (str(hp), False) for hp, cp in mounts})
                expected_mounts.update({cp: (str(hp), request['stage'] == 'dependency') for hp, cp in artifact_mounts})
                if artifacts is not None and request['stage'] == 'verify':
                    expected_mounts['/work/artifacts'] = (str(artifacts), False)
                if actual_mounts != expected_mounts:
                    problems.append('verification_mounts')
                actual_config = info.get('Config') or {}
                if dep_network and set(((info.get('NetworkSettings') or {}).get('Networks') or {})) != {dep_network}:
                    problems.append('verification_networks')
                expected_cmd = render_command(runner['command'], {'model': '', 'job_id': job_id, 'session_arg': '', 'verify': req['verify']})
                if (actual_config.get('Image') != image or actual_config.get('Cmd') != expected_cmd
                        or actual_config.get('User') != f'{os.getuid()}:{os.getgid()}'
                        or 'ALL' not in (host.get('CapDrop') or [])
                        or 'no-new-privileges' not in (host.get('SecurityOpt') or [])):
                    problems.append('verification_configuration')
                actual_env = actual_config.get('Env')
                required_env = {'TMPDIR': '/tmp-hdd', 'HOME': '/tmp-hdd/home',
                                'HTTP_PROXY': f'http://job-{job_id}@{PROXY_HOST}:{PROXY_PORT}',
                                'HTTPS_PROXY': f'http://job-{job_id}@{PROXY_HOST}:{PROXY_PORT}', 'NO_PROXY': PROXY_HOST}
                if dep_network:
                    required_env.update(HTTP_PROXY='http://verify-proxy:3128', HTTPS_PROXY='http://verify-proxy:3128', NO_PROXY='verify-proxy')
                required_env.update(render_env(runner.get('env') or {}, {'model': '', 'job_id': job_id, 'session_arg': '', 'verify': req['verify']}))
                if not isinstance(actual_env, list) or any(not isinstance(x, str) or '=' not in x for x in actual_env):
                    problems.append('verification_environment')
                else:
                    env_pairs = [x.split('=', 1) for x in actual_env]
                    for key, value in required_env.items():
                        if [v for k, v in env_pairs if k == key] != [value]:
                            problems.append('verification_environment')
                    image_env_names = {'PATH', 'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'TZ', 'HOSTNAME', 'GPG_KEY',
                                       'PYTHON_VERSION', 'PYTHON_SHA256', 'PYTHON_PIP_VERSION', 'PYTHON_SETUPTOOLS_VERSION',
                                       'PYTHON_GET_PIP_URL', 'PYTHON_GET_PIP_SHA256', 'PYTHONDONTWRITEBYTECODE',
                                       'PYTHONUNBUFFERED', 'NODE_VERSION', 'YARN_VERSION', 'DOTNET_VERSION',
                                       'ASPNET_VERSION', 'DOTNET_RUNNING_IN_CONTAINER', 'DOTNET_SYSTEM_GLOBALIZATION_INVARIANT'}
                    allowed_env_names = image_env_names | set(required_env)
                    if any(k not in allowed_env_names for k, _ in env_pairs):
                        problems.append('verification_environment')
                    if any(k in ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL') for k, _ in env_pairs):
                        problems.append('verification_environment')
                if actual_config.get('WorkingDir') != '/work' or actual_config.get('Entrypoint') not in (None, []):
                    problems.append('verification_configuration')
                logconfig = host.get('LogConfig') or {}
                if host.get('NetworkMode') != (dep_network or 'none') or (dep_network and host.get('Dns') != ['127.0.0.1']) or logconfig.get('Type') != 'json-file' or (logconfig.get('Config') or {}).get('max-size') not in (None, '-1') or (logconfig.get('Config') or {}).get('mode') != 'blocking':
                    problems.append('verification_configuration')
            fd = self._verify_lock(job_id)
            try:
                record = self._verify_load(job_id)
                deadline = _dt.datetime.fromisoformat(record['deadline_at'])
                if _dt.datetime.now(_dt.timezone.utc) >= deadline:
                    record['timed_out'] = True
                if record['timed_out'] or record['state'] != 'starting' or problems:
                    self.docker.run(['stop', '-t', '0', 'ax-verify-' + job_id], timeout=30)
                    record['state'] = 'unknown'
                    self._verify_save(job_id, record)
                    if dep_network:
                        self._verify_stop_proxy(job_id)
                    raise internal('isolation_unconfirmed')
                initial_settings = verify_inspection_settings(info)
                if dep_network:
                    proxy_info = self.docker.inspect('ax-vp-' + job_id)
                    if proxy_info is None:
                        raise internal('verification_proxy_unconfirmed')
                    record['proxy_settings'] = verify_inspection_settings(proxy_info)
                started = self.docker.run(['start', 'ax-verify-' + job_id], timeout=180)
                if started.returncode:
                    record['state'] = 'unknown'
                    self._verify_save(job_id, record)
                    raise internal('docker_start_failed')
                running_info = self.docker.inspect('ax-verify-' + job_id)
                if (running_info is None or verify_inspection_settings(running_info) != initial_settings
                        or (dep_network and (set(((running_info.get('NetworkSettings') or {}).get('Networks') or {})) != {dep_network}
                            or not self._verify_proxy_still_safe(job_id, record['proxy_settings'])))):
                    self.docker.run(['stop', '-t', '0', 'ax-verify-' + job_id], timeout=30)
                    record['state'] = 'unknown'
                    self._verify_save(job_id, record)
                    if dep_network:
                        self._verify_stop_proxy(job_id)
                    raise internal('isolation_unconfirmed')
                record.update(state='running', executed=True, isolation_verified=True,
                              home_absent=True, credentials_absent=True, host_config_absent=True, network_enforced=True,
                              inspection_settings=initial_settings,
                              started_at=(running_info.get('State') or {}).get('StartedAt'))
                self._verify_save(job_id, record)
                return {'job_id': job_id, 'state': 'running'}
            finally:
                os.close(fd)

        except Exception:
            if request['stage'] == 'dependency':
                self._verify_stop_proxy(job_id)
            fd = self._verify_lock(job_id)
            try:
                failed_record = self._verify_load(job_id)
                if failed_record.get('state') == 'starting':
                    failed_record['state'] = 'unknown'
                    self._verify_save(job_id, failed_record)
            finally:
                os.close(fd)
            raise
        finally:
            os.close(work_lock)

    def job_verify_expire(self, job_id):
        fd = self._verify_lock(job_id)
        try:
            record = self._verify_load(job_id)
            if record['state'] == 'exited':
                return {'job_id': job_id, 'state': 'exited'}
            info = self.docker.inspect('ax-verify-' + job_id)
            if info is not None and (info.get('State') or {}).get('Status') not in ('exited', 'dead'):
                stop = self.docker.run(['stop', '-t', '0', 'ax-verify-' + job_id], timeout=30)
                if stop.returncode:
                    raise internal('verification_stop_failed')
            record['timed_out'] = True
            if record.get('stage') == 'dependency':
                self._verify_stop_proxy(job_id)
            self._verify_save(job_id, record)
            return {'job_id': job_id, 'state': 'unknown'}
        finally:
            os.close(fd)

    def job_verify_result(self, job_id):
        fd = self._verify_lock(job_id)
        try:
            record = self._verify_load(job_id)
            if record['state'] == 'exited':
                if record.get('log_complete'):
                    path = self._verify_path(job_id).with_suffix('.log')
                    try:
                        fd_log = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        with os.fdopen(fd_log, 'r', encoding='utf-8') as stream:
                            valid = stat.S_ISREG(os.fstat(stream.fileno()).st_mode) and stream.read() == record.get('log')
                    except (OSError, UnicodeError):
                        valid = False
                    if not valid:
                        record.update(log_complete=False, output_truncated=True)
                        self._verify_save(job_id, record)
                return record
            if record['state'] == 'unknown':
                return record
            if not record.get('timed_out'):
                try:
                    deadline = _dt.datetime.fromisoformat(record['deadline_at'])
                    deadline_reached = _dt.datetime.now(_dt.timezone.utc) >= deadline
                except (KeyError, TypeError, ValueError):
                    record.update(state='unknown', isolation_verified=False, home_absent=False, credentials_absent=False, host_config_absent=False, network_enforced=False)
                    self._verify_save(job_id, record)
                    return record
                if deadline_reached:
                    deadline_info = self.docker.inspect('ax-verify-' + job_id)
                    if deadline_info is None:
                        record.update(state='unknown', isolation_verified=False, home_absent=False, credentials_absent=False, host_config_absent=False, network_enforced=False)
                        self._verify_save(job_id, record)
                        return record
                    if (deadline_info.get('State') or {}).get('Status') not in ('exited', 'dead'):
                        stopped = self.docker.run(['stop', '-t', '0', 'ax-verify-' + job_id], timeout=30)
                        record.update(timed_out=True, state='unknown')
                        self._verify_save(job_id, record)
                        if stopped.returncode:
                            if record.get('stage') == 'dependency':
                                self._verify_stop_proxy(job_id)
                            return record
                runtime = '/run/user/' + str(os.getuid())
                env = dict(self.cfg.env, XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS='unix:path=' + runtime + '/bus')
                try:
                    monitor = subprocess.run(['systemctl', '--user', 'is-active', 'ax-verify-' + job_id + '.timer'],
                                             env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15)
                    monitor_ok = monitor.returncode == 0 and monitor.stdout.strip() == 'active'
                except (OSError, subprocess.SubprocessError):
                    monitor_ok = False
                if not monitor_ok and not record.get('timed_out'):
                    info = self.docker.inspect('ax-verify-' + job_id)
                    if info is not None and (info.get('State') or {}).get('Status') not in ('exited', 'dead'):
                        self.docker.run(['stop', '-t', '0', 'ax-verify-' + job_id], timeout=30)
                    record['state'] = 'unknown'
                    if record.get('stage') == 'dependency':
                        self._verify_stop_proxy(job_id)
                    self._verify_save(job_id, record)
                    return record
            info = self.docker.inspect('ax-verify-' + job_id)
            if info is None:
                if record.get('state') == 'starting' and not record.get('timed_out'):
                    return record
                record['state'] = 'unknown'
                if record.get('stage') == 'dependency':
                    self._verify_stop_proxy(job_id)
                self._verify_save(job_id, record)
                return record
            if record.get('stage') == 'dependency' and not record.get('timed_out') and not self._verify_proxy_still_safe(job_id, record.get('proxy_settings')):
                if (info.get('State') or {}).get('Status') not in ('exited', 'dead'):
                    self.docker.run(['stop', '-t', '0', 'ax-verify-' + job_id], timeout=30)
                self._verify_stop_proxy(job_id)
                record.update(state='unknown', isolation_verified=False, home_absent=False, credentials_absent=False, host_config_absent=False, network_enforced=False)
                self._verify_save(job_id, record)
                return record
            if not record.get('inspection_settings') or verify_inspection_settings(info) != record['inspection_settings'] or (record.get('stage') == 'dependency' and set(((info.get('NetworkSettings') or {}).get('Networks') or {})) != {'ax-vj-' + job_id}):
                if (info.get('State') or {}).get('Status') not in ('exited', 'dead'):
                    self.docker.run(['stop', '-t', '0', 'ax-verify-' + job_id], timeout=30)
                if record.get('stage') == 'dependency':
                    self._verify_stop_proxy(job_id)
                record.update(state='unknown', isolation_verified=False, home_absent=False, credentials_absent=False, host_config_absent=False, network_enforced=False)
                self._verify_save(job_id, record)
                return record
            state = info.get('State') or {}
            if state.get('Status') not in ('exited', 'dead'):
                return record
            try:
                finished = _dt.datetime.fromisoformat(state['FinishedAt'].replace('Z', '+00:00'))
                deadline = _dt.datetime.fromisoformat(record['deadline_at'])
                if finished.tzinfo is None or type(state.get('ExitCode')) is not int or type(state.get('OOMKilled')) is not bool:
                    raise ValueError('incomplete exit state')
                record['timed_out'] = record['timed_out'] or finished > deadline
            except (KeyError, TypeError, ValueError):
                record['state'] = 'unknown'
                self._verify_save(job_id, record)
                return record
            logs = self.docker.run(['logs', 'ax-verify-' + job_id], merge_stderr=True)
            if logs.returncode == 0:
                path = self._verify_path(job_id).with_suffix('.log')
                _atomic_write(path, logs.stdout or '', 0o600)
                record.update(log=logs.stdout or '', log_complete=True, output_truncated=False)
            record.update(state='exited', exit_code=state.get('ExitCode'), oom_killed=state.get('OOMKilled'),
                          ended_at=state.get('FinishedAt'))
            if record.get('stage') == 'dependency':
                self._verify_stop_proxy(job_id)
                if (record['exit_code'] == 0 and record['oom_killed'] is False and not record['timed_out']
                        and record['log_complete'] and record['isolation_verified']):
                    directory = self.cfg.state_dir / 'verification' / record['run_id']
                    marker = directory / (record['workdir_id'] + '-' + record['step'] + '.json')
                    identity = {k: record[k] for k in ('project', 'target', 'revision', 'run_id', 'attempt', 'recipe_id', 'recipe_snapshot_id')}
                    identity['job_id'] = job_id
                    self._verify_save(job_id, record)
                    _atomic_write(marker, json.dumps(identity, sort_keys=True), 0o600)
            self._verify_save(job_id, record)
            return record
        finally:
            os.close(fd)


    # --- 記録 ---
    def _rec(self, job_id: str, suffix: str) -> Path:
        return self.cfg.jobs_dir / f"{job_id}.{suffix}"

    def _read_rec(self, job_id: str, suffix: str) -> str | None:
        try:
            v = self._rec(job_id, suffix).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return v or None

    @staticmethod
    def _chat_io():
        import importlib.util
        spec = importlib.util.spec_from_file_location('ax_chat_io', Path(__file__).with_name('ax_chat_io.py'))
        if spec is None or spec.loader is None:
            raise internal('chat_io_unavailable')
        module = importlib.util.module_from_spec(spec)
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
        return module

    def job_chat_result(self, job_id: str) -> dict:
        self._require_known(job_id)
        try:
            record = json.loads(self._read_rec(job_id, 'json') or '')
            exit_record = json.loads(self._read_rec(job_id, 'exit.json') or '')
            wd = self._read_rec(job_id, 'wd')
            if (record.get('job_id') != job_id or record.get('kind') != 'chat-json'
                    or record.get('tool_free_evidence') != {'job_id': job_id, 'runner': record.get('runner'), 'profile': 'tool-free-chat-v1', 'request_tools_absent': True, 'inspect_verified': True, 'trust_basis': 'host_managed_runner_and_image'}
                    or record.get('wd') != wd or not isinstance(wd, str) or not ID_RE.fullmatch(wd)
                    or exit_record.get('job_id') != job_id or exit_record.get('state') != 'exited'
                    or type(exit_record.get('exit_code')) is not int or exit_record['exit_code'] != 0):
                raise ValueError('chat job not successfully exited')
            return {'job_id': job_id, 'tool_free': True, 'tool_free_evidence': record['tool_free_evidence'], **self._chat_io().collect(self.cfg.work_of(wd))}
        except Exception:
            raise rejected('chat_response_unavailable') from None

    def job_report(self, job_id: str) -> dict:
        wd = self._read_rec(job_id, "wd")
        if not wd or not ID_RE.fullmatch(wd):
            raise not_found("workdir_not_found")
        path = self.cfg.work_of(wd) / "research-report.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("not a regular file")
                data = source.read(1048577)
            if len(data) > 1048576:
                raise ValueError("too large")
            return {"job_id": job_id, "report": data.decode("utf-8")}
        except (OSError, ValueError, UnicodeError):
            raise rejected("report_unavailable") from None

    # --- job-start ---
    def job_start(self, job_id: str, stdin_text: str) -> dict:
        cfg = self.cfg
        req = parse_request(stdin_text, job_id)
        runner = load_runner(cfg, req["runner"])
        kind = runner["kind"]
        if "safety_profile" in req and (req["safety_profile"] != "tool-free-chat-v1" or kind != "chat-json"):
            raise rejected("bad_field", "invalid safety profile")
        if kind == "chat-json":
            if any(req.get(k) for k in ("env_names", "ro_mounts", "target_ro", "snapshot_id", "resume_from", "workdir_id", "session_id")):
                raise rejected("bad_field", "chat-json は固定の実行環境だけ使える")
            if set((runner.get("env") or {}).keys()) - {"AX_CHAT_JSON_DIR"} or (runner.get("env") or {}).get("AX_CHAT_JSON_DIR", "/work") != "/work":
                raise internal("runner_invalid", "chat-json の環境が不正")
            runner = {**runner, "command": "python -P -m axcommon.chat_json"}
        if kind in KINDS_WITH_MODEL and "model" not in req:
            raise rejected("missing_field", "model が要る")
        if "verify" in req and "{verify}" not in runner["command"]:
            raise rejected("bad_field", "この runner は verify を受けない")
        if kind == "verify" and "{verify}" in runner["command"] and "verify" not in req:
            raise rejected("missing_field", "verify が要る")
        if "tools" in req and kind != "agent-cli":
            raise rejected("bad_field", "tools は agent-cli だけ")
        # env_names（runner の定義に書かれた名前だけ）
        allowed_env = set(runner.get("allowed_env_names") or [])
        extra_env: list[str] = []
        env_values: dict[str, str] = {}
        for n in req.get("env_names") or []:
            if n not in allowed_env or n in RESERVED_ENV:
                raise rejected("env_name_not_allowed", "runner の定義にない環境変数名")
            extra_env.append(n)
        image = resolve_image(cfg, runner)
        ro = resolve_ro_mounts(cfg, req.get("ro_mounts") or [])
        target_ro = None
        if req.get("snapshot_id"):
            _, target_ro = _snapshot_manifest(cfg, req["snapshot_id"])
        if "target_ro" in req:
            target_ro = check_host_path(cfg, req["target_ro"], "target_ro")
            if not target_ro.is_dir():
                raise LauncherError(EXIT_NOT_FOUND, "not_found", "mount_missing", "target_ro が存在するディレクトリでない")

        # 連鎖（root）と作業の場所（wd）。S-8.12、F-18.2
        resume_from = req.get("resume_from")
        explicit_wd = req.get("workdir_id")
        root = job_id
        wd_from = None
        if resume_from:
            root_rec = self._read_rec(resume_from, "root")
            if root_rec is None:
                raise not_found("session_not_found", "resume_from の連鎖の記録が無い")
            root = root_rec
            wd_from = self._read_rec(resume_from, "wd")
            if explicit_wd and wd_from and explicit_wd != wd_from:
                raise rejected("workdir_mismatch", "resume_from と workdir_id が違う場所を指す")
        wd = explicit_wd or wd_from or job_id
        session_id = req.get("session_id")
        session_arg = ""
        if resume_from:
            if not session_id:
                raise rejected("session_id_required", "resume_from には session_id が要る")
            transcript = cfg.chain_of(root) / "home" / ".claude" / "projects" / "-work" / f"{session_id}.jsonl"
            if not cfg.chain_of(root).is_dir() or not transcript.is_file():
                raise not_found("session_not_found", "会話の記録が無い")
            session_arg = f"--resume {session_id}"
        elif session_id:
            session_arg = f"--session-id {session_id}"
        work = cfg.work_of(wd)
        if explicit_wd and not work.is_dir():
            raise not_found("workdir_not_found", "workdir_id が指す場所が無い")

        # 秘密（model-gateway-key と、許された env_names）
        child_env: dict[str, str] = {}
        preserve: list[str] = []
        if kind in KINDS_WITH_MODEL:
            try:
                child_env["ANTHROPIC_AUTH_TOKEN"] = (cfg.secrets_dir / "model-gateway-key").read_text(encoding="utf-8").strip()
            except OSError as e:
                raise internal("launcher_unavailable", "model-gateway-key を読めない") from e
            if not child_env["ANTHROPIC_AUTH_TOKEN"]:
                raise internal("launcher_unavailable", "model-gateway-key が空")
            preserve.append("ANTHROPIC_AUTH_TOKEN")
        for n in extra_env:
            try:
                env_values[n] = (cfg.secrets_dir / "job-env" / n).read_text(encoding="utf-8").strip()
            except OSError as e:
                raise internal("launcher_unavailable", f"環境変数 {n} の値を読めない") from e
            child_env[n] = env_values[n]
            preserve.append(n)

        # ここから作る。無いときだけ operator の権限で作る
        cfg.jobs_dir.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        chain = cfg.chain_of(root)
        chain.mkdir(parents=True, exist_ok=True)
        home = chain / "home"
        home.mkdir(mode=0o700, exist_ok=True)
        os.chmod(home, 0o700)

        locks = Locks()
        try:
            locks.take(cfg.jobs_dir / f".wd-{wd}.lock", "busy")
            locks.take(chain / ".chain.lock", "busy")
            if self.docker.ps_ids(f"label=ax.wd={wd}"):
                raise busy("workdir_busy", "同じ /work で実行中のコンテナがある")
            if self.docker.ps_ids(f"label=ax.chain={root}"):
                raise busy("session_busy", "同じ連鎖で実行中のコンテナがある")

            if kind == 'chat-json':
                try:
                    self._chat_io().prepare(work, req)
                    chat_request = json.loads((work / 'request.json').read_text(encoding='utf-8'))
                    if not isinstance(chat_request, dict) or set(chat_request) - {'model', 'max_tokens', 'messages'} or 'tools' in chat_request:
                        raise ValueError('unexpected chat request')
                except Exception:
                    raise rejected('chat_request_invalid') from None
            _atomic_write(self._rec(job_id, "wd"), wd + "\n")
            _atomic_write(self._rec(job_id, "root"), root + "\n")
            prompt_dir = work / ".ax-prompt"
            if prompt_dir.is_symlink():
                raise rejected("bad_prompt_directory")
            prompt_dir.mkdir(mode=0o700, exist_ok=True)
            _atomic_write(prompt_dir / "user.md", req["prompt"], 0o600)
            record = {k: v for k, v in req.items() if k not in ("verify", "prompt")}
            record.update({"root": root, "wd": wd, "image": image, "started_at": utcnow(), "kind": kind})
            _atomic_write(self._rec(job_id, "json"), json.dumps(record, ensure_ascii=False) + "\n")
            if session_id:
                _atomic_write(self._rec(job_id, "session"), session_id + "\n")

            # 許可先（egress-proxy が ro で読む）。ゲートの行は tools を宣言したジョブにだけ
            lines = [h for h in dict.fromkeys(req["allow_hosts"])]
            if req.get("tools"):
                lines.append(f"@gate {cfg.gate_port}")
            cfg.egress_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(cfg.egress_dir, 0o755)
            _atomic_write(cfg.egress_dir / f"job-{job_id}.allow", "\n".join(lines) + ("\n" if lines else ""), 0o644)

            if kind == "agent-cli":
                settings = home / ".claude" / "settings.json"
                if not settings.exists():
                    settings.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write(settings, json.dumps(AGENT_SETTINGS) + "\n", 0o600)

            args = build_run_args(cfg, req, runner, image=image, root=root, wd=wd, session_arg=session_arg,
                                  ro_mounts=ro, target_ro=target_ro, extra_env_names=extra_env)
            if kind == "chat-json":
                args = ["create"] + args[2:]
            r = self.docker.run(args, env=child_env, preserve=preserve, timeout=180)
            if r.returncode != 0:
                raise internal("docker_run_failed", "docker run が失敗した")

            expect = {"/work", "/tmp-hdd"} | ({"/target"} if target_ro else set()) | {cp for _, cp in ro}
            info = self.docker.inspect(f"ax-job-{job_id}")
            problems = ["inspect_failed"] if info is None else check_isolation(info, cfg, expect_mounts=expect)
            if kind == "chat-json" and info is not None:
                problems.extend(check_chat_isolation(info, cfg, image, wd, root))
            if req.get("snapshot_id") and info is not None:
                mounted = [m for m in info.get("Mounts", []) if m.get("Destination") == "/target"]
                if len(mounted) != 1 or mounted[0].get("RW") is not False or mounted[0].get("Source") != str(target_ro):
                    problems.append("snapshot_not_readonly")
            if problems:
                self.docker.run(["stop", "-t", "5", f"ax-job-{job_id}"])
                raise internal("isolation_unconfirmed", "隔離を確認できないので止めた: " + ",".join(problems))
            if kind == "chat-json":
                record["tool_free_evidence"] = {"job_id": job_id, "runner": req["runner"], "profile": "tool-free-chat-v1", "request_tools_absent": True, "inspect_verified": True, "trust_basis": "host_managed_runner_and_image"}
                _atomic_write(self._rec(job_id, "json"), json.dumps(record, ensure_ascii=False) + "\n")
                started = self.docker.run(["start", f"ax-job-{job_id}"], timeout=180)
                if started.returncode:
                    raise internal("docker_start_failed")
                running_info = self.docker.inspect(f"ax-job-{job_id}")
                if running_info is None or check_isolation(running_info, cfg, expect_mounts=expect) or check_chat_isolation(running_info, cfg, image, wd, root):
                    self.docker.run(["stop", "-t", "0", f"ax-job-{job_id}"], timeout=30)
                    record.pop("tool_free_evidence", None)
                    _atomic_write(self._rec(job_id, "json"), json.dumps(record, ensure_ascii=False) + "\n")
                    raise internal("isolation_unconfirmed")
        finally:
            locks.release()
        return {"job_id": job_id, "root": root, "wd": wd}

    # --- 終了処理 ---
    def _finalize(self, job_id: str, info: dict | None) -> dict:
        """ログとusage を保存し、終了済みコンテナを保持する。冪等。"""
        cfg = self.cfg
        exit_rec = self._read_rec(job_id, "exit.json")
        if exit_rec:
            try:
                return json.loads(exit_rec)
            except ValueError:
                pass
        wd = self._read_rec(job_id, "wd") or job_id
        root = self._read_rec(job_id, "root") or job_id
        state = (info or {}).get("State") or {}
        result = {"job_id": job_id, "state": "exited", "exit_code": state.get("ExitCode"), "oom_killed": bool(state.get("OOMKilled")),
                  "finished_at": state.get("FinishedAt") or utcnow(), "root": root, "wd": wd}
        work = cfg.work_of(wd)
        if info is not None:
            r = self.docker.run(["logs", f"ax-job-{job_id}"], merge_stderr=True)
            if r.returncode == 0 and work.is_dir():
                _atomic_write(work / f".container-{job_id}.log", r.stdout or "", 0o644)
        sid = self._read_rec(job_id, "session")
        if sid and work.is_dir():
            usage = extract_usage(cfg.chain_of(root) / "home" / ".claude" / "projects" / "-work" / f"{sid}.jsonl")
            if usage:
                _atomic_write(work / f".usage-{job_id}.json", json.dumps(usage) + "\n", 0o644)
        _atomic_write(self._rec(job_id, "exit.json"), json.dumps(result) + "\n")
        allow = cfg.egress_dir / f"job-{job_id}.allow"
        if allow.exists():
            arch = cfg.egress_dir / "archive"
            arch.mkdir(parents=True, exist_ok=True)
            shutil.move(str(allow), str(arch / f"job-{job_id}.allow.{utcnow()}"))  # 削除しない
        return result

    def _require_known(self, job_id: str) -> None:
        if not self._rec(job_id, "json").is_file():
            raise not_found("job_not_found", "そのジョブの記録が無い")

    # --- job-status ---
    def job_status(self, job_id: str) -> dict:
        self._require_known(job_id)
        info = self.docker.inspect(f"ax-job-{job_id}")
        if info is None:
            rec = self._read_rec(job_id, "exit.json")
            if rec:
                return json.loads(rec)
            return {"job_id": job_id, "state": "gone", "exit_code": None}
        st = info.get("State") or {}
        status = str(st.get("Status", "unknown"))
        if status in ("exited", "dead"):
            return self._finalize(job_id, info)
        return {"job_id": job_id, "state": status, "exit_code": None, "started_at": st.get("StartedAt"),
                "root": self._read_rec(job_id, "root"), "wd": self._read_rec(job_id, "wd")}

    def job_log(self, job_id: str, limit: int) -> dict:
        self._require_known(job_id)
        if not 1 <= limit <= 131072:
            raise rejected("bad_limit")
        wd = self._read_rec(job_id, "wd") or job_id
        path = self.cfg.work_of(wd) / f".container-{job_id}.log"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - limit))
                text = f.read(limit).decode("utf-8", errors="replace")
        except OSError as exc:
            raise not_found("log_unavailable") from exc
        return {"job_id": job_id, "log": text}

    # --- job-stop ---
    def job_stop(self, job_id: str) -> dict:
        self._require_known(job_id)
        info = self.docker.inspect(f"ax-job-{job_id}")
        if info is None:
            rec = self._read_rec(job_id, "exit.json")
            if rec:
                return json.loads(rec)
            return {"job_id": job_id, "state": "gone", "exit_code": None}
        r = self.docker.run(["stop", "-t", "20", f"ax-job-{job_id}"], timeout=60)
        if r.returncode != 0:
            raise internal("docker_stop_failed", "docker stop が失敗した")
        info2 = self.docker.inspect(f"ax-job-{job_id}")
        res = self._finalize(job_id, info2)
        res["stopped"] = True
        return res

    # --- job-list ---
    def job_list(self) -> dict:
        r = self.docker.run(["ps", "-a", "--filter", "label=ax.managed=1", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"])
        if r.returncode != 0:
            raise internal("docker_ps_failed", "docker ps が失敗した")
        jobs = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or not parts[0].startswith("ax-job-"):
                continue
            jobs.append({"job_id": parts[0][len("ax-job-"):], "name": parts[0], "state": parts[1], "status": parts[2] if len(parts) > 2 else ""})
        return {"jobs": jobs}

    # --- job-retire ---
    def job_retire(self, job_id: str) -> dict:
        cfg = self.cfg
        root = self._read_rec(job_id, "root")
        if root is None:
            raise not_found("job_not_found", "そのジョブの連鎖の記録が無い")
        if self.docker.ps_ids(f"label=ax.chain={root}"):
            raise busy("chain_running", "その連鎖に実行中のコンテナがある")
        chain = cfg.chain_of(root)
        if not chain.is_dir():
            return {"job_id": job_id, "root": root, "archived": None}
        dest_dir = cfg.tmp_dir / "archive"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"job-{root}-{utcnow()}"
        os.rename(str(chain), str(dest))  # 同じファイルシステム内の移動。削除しない
        return {"job_id": job_id, "root": root, "archived": str(dest)}


# ---------------------------------------------------------------- secret-put

def secret_put(cfg: Config, name: str, stdin_read) -> dict:
    """Install a permitted target without exposing its contents in diagnostics."""
    manifest = _load_yaml(cfg.env_home / "manifest.yaml")
    if not isinstance(manifest, dict):
        raise internal("manifest_invalid")
    constraints = manifest.get("constraints", {})
    if not isinstance(constraints, dict):
        raise internal("manifest_invalid")
    stops = constraints.get("stop_files", [])
    if not isinstance(stops, list) or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", x) or x in (".", "..") for x in stops):
        raise internal("manifest_invalid")
    if os.path.lexists(cfg.env_home / "DETACHED") or any(os.path.lexists(cfg.env_home / x) for x in stops):
        raise LauncherError(EXIT_DETACHED, "detached", "stopped")
    targets = manifest.get("secret_targets", [])
    if not isinstance(targets, list):
        raise internal("manifest_invalid")
    found = [x for x in targets if isinstance(x, dict) and x.get("name") == name]
    if len(found) != 1:
        raise rejected("target_not_allowed")
    target = found[0]
    raw_path, max_bytes = target.get("path"), target.get("max_bytes")
    if (not isinstance(raw_path, str) or not (raw_path.startswith("/") or raw_path.startswith("~/"))
            or str(target.get("mode")) != "600" or type(max_bytes) is not int or not 1 <= max_bytes <= 65536):
        raise internal("manifest_invalid")
    path = Path(str(cfg.home) + raw_path[1:] if raw_path.startswith("~/") else raw_path)
    if path.parent != Path(os.path.abspath(path.parent)) or path.parent.resolve() != path.parent:
        raise rejected("unsafe_parent")
    if not path.parent.is_dir():
        raise not_found("target_dir_missing")
    if path.is_symlink():
        raise rejected("unsafe_target")
    data = stdin_read(max_bytes + 1)
    if not isinstance(data, bytes) or not 0 < len(data) <= max_bytes:
        raise rejected("invalid_size")
    archived = None
    temp_path = None
    archive = path.parent / "archive"
    try:
        if path.exists():
            if not path.is_file() or archive.is_symlink():
                raise rejected("unsafe_target")
            archive.mkdir(mode=0o700, exist_ok=True)
            if not archive.is_dir():
                raise rejected("unsafe_archive")
            os.chmod(archive, 0o700)
            archived = f"{path.name}.{utcnow()}.{uuid.uuid4().hex}"
            dest = archive / archived
            shutil.copy2(path, dest, follow_symlinks=False)
            os.chmod(dest, 0o600)
        fd, temp_name = tempfile.mkstemp(prefix=".secret-put-", dir=path.parent)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        temp_path = None
    except BaseException:
        if temp_path is not None:
            try:
                if not archive.is_symlink():
                    archive.mkdir(mode=0o700, exist_ok=True)
                    os.chmod(archive, 0o700)
                    os.replace(temp_path, archive / (".failed-" + utcnow() + "-" + uuid.uuid4().hex))
            except OSError:
                pass
        raise
    result = {"ok": True, "target": name, "size": len(data), "mtime": utcnow(), "mode": "600", "archived": archived}
    logdir = cfg.env_home / "logs"
    try:
        logdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (logdir / "secret-put.log").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"time": utcnow(), "target": name, "size": len(data), "result": "ok"}) + "\n")
    except OSError:
        pass
    return result


# ---------------------------------------------------------------- 入口
def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def parse_invocation(argv: list[str], environ: dict) -> tuple[str, list[str]]:
    """(caller, tokens)。tokens は [サブコマンド, 引数…]。SSH_ORIGINAL_COMMAND があればそれが正で、argv の残りは受けない。"""
    caller = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--caller":
            if i + 1 >= len(argv):
                raise rejected("bad_args", "--caller に値が要る")
            if caller is not None:
                raise rejected("bad_args", "--caller が 2 回ある")
            caller = argv[i + 1]
            i += 2
        elif a.startswith("--caller="):
            if caller is not None:
                raise rejected("bad_args", "--caller が 2 回ある")
            caller = a.split("=", 1)[1]
            i += 1
        else:
            rest.append(a)
            i += 1
    if caller is None:
        caller = "env"  # 省略は env と同じ扱い（F-18.2）。launcher は強制コマンドで固定する
    if caller not in CALLERS:
        raise rejected("unknown_caller", "知らない呼び出し元の名前")
    orig = environ.get("SSH_ORIGINAL_COMMAND")
    if orig is not None:
        if rest:
            raise rejected("bad_args", "強制コマンドの下では引数を受けない")
        tokens = orig.split()
        # LauncherClient は 'ax-env-run --caller launcher <sub> <args>' 全体を送る。
        # 強制コマンドの下では先頭の呼び出し名と --caller の組を捨てて、サブコマンドから読む。
        while tokens and (tokens[0] == "ax-env-run" or tokens[0].endswith("/ax-env-run")):
            tokens = tokens[1:]
        if len(tokens) >= 2 and tokens[0] == "--caller":
            tokens = tokens[2:]
    else:
        tokens = rest
    return caller, tokens


def dispatch(argv: list[str], environ: dict, stdin_read) -> tuple[int, dict]:
    try:
        caller, tokens = parse_invocation(argv, environ)
        if not tokens:
            raise rejected("no_subcommand", "サブコマンドが無い")
        sub, args = tokens[0], tokens[1:]
        if caller == "env":
            if sub in LAUNCHER_SUBCOMMANDS:
                raise rejected("caller_not_allowed", "この呼び出し元は job-* を使えない")
            if sub == "version":
                if args:
                    raise rejected("bad_args")
                import importlib.util
                sys.dont_write_bytecode = True
                module_spec = importlib.util.spec_from_file_location("env_version", Path(__file__).with_name("env_version.py"))
                env_version = importlib.util.module_from_spec(module_spec)
                module_spec.loader.exec_module(env_version)
                return EXIT_OK, env_version.version(Path(__file__).resolve().parent.parent)
            if sub in {"submit", "status", "fetch"}:
                import importlib.util
                module_spec = importlib.util.spec_from_file_location("env_jobs", Path(__file__).with_name("env_jobs.py"))
                env_jobs = importlib.util.module_from_spec(module_spec)
                module_spec.loader.exec_module(env_jobs)
                if len(args) not in ((1, 2) if sub == "fetch" else (1,)):
                    raise rejected("bad_args")
                cfg = Config(environ)
                if (cfg.env_home / "DETACHED").exists():
                    raise LauncherError(EXIT_DETACHED, "detached", "detached")
                try:
                    if sub == "submit":
                        return EXIT_OK, env_jobs.submit(cfg.env_home, args[0], stdin_read(1100000))
                    if sub == "status":
                        return EXIT_OK, env_jobs.status(cfg.env_home, args[0])
                    return EXIT_OK, env_jobs.fetch(cfg.env_home, args[0], args[1] if len(args) == 2 else None)
                except env_jobs.JobError as exc:
                    if sub == "status" and exc.missing:
                        error = not_found(exc.reason).as_json()
                        error.update(job_id=args[0], state="unknown", exit_code=None)
                        return EXIT_NOT_FOUND, error
                    raise (not_found(exc.reason) if exc.missing else rejected(exc.reason)) from None
            if sub == "probe":
                if args:
                    raise rejected("bad_args")
                import importlib.util
                module_spec = importlib.util.spec_from_file_location("env_probe", Path(__file__).with_name("env_probe.py"))
                env_probe = importlib.util.module_from_spec(module_spec)
                module_spec.loader.exec_module(env_probe)
                return EXIT_OK, env_probe.inspect(Config(environ).env_home, environ, stdin_read(4097))
            if sub == "secret-put":
                if len(args) != 1 or not ID_RE.fullmatch(args[0]):
                    raise rejected("bad_args")
                return EXIT_OK, secret_put(Config(environ), args[0], stdin_read)
            if sub in ENV_SUBCOMMANDS:
                raise internal("not_implemented", "ENV-MODULE §5 のサブコマンドは B14b で実装する（B6 の最小版は job-* だけ）")
            raise rejected("unknown_subcommand", "知らないサブコマンド")
        # launcher
        if sub in ENV_SUBCOMMANDS:
            raise rejected("caller_not_allowed", "この呼び出し元は job-* だけ使える")
        if sub not in LAUNCHER_SUBCOMMANDS:
            raise rejected("unknown_subcommand", "知らないサブコマンド")
        if sub == "job-log":
            if len(args) != 2 or not ID_RE.fullmatch(args[0]) or not args[1].isdigit() or len(args[1]) > 6:
                raise rejected("bad_args")
        elif sub == "job-list":
            if args:
                raise rejected("bad_args", "job-list は引数を取らない")
        else:
            if len(args) != 1 or not ID_RE.match(args[0]):
                raise rejected("bad_args", "job_id が 1 つ要る（[a-z0-9-]{1,40}）")
        cfg = Config(environ)
        if (cfg.env_home / "DETACHED").exists():
            raise LauncherError(EXIT_DETACHED, "detached", "detached", "取り外し済み")
        la = Launcher(cfg)
        if sub == "job-verify-recipe":
            return EXIT_OK, la.job_verify_recipe(args[0], stdin_read())
        if sub == "job-verify-prepare":
            return EXIT_OK, la.job_verify_prepare(args[0], stdin_read())
        if sub == "job-verify-result":
            return EXIT_OK, la.job_verify_result(args[0])
        if sub == "job-verify-expire":
            return EXIT_OK, la.job_verify_expire(args[0])
        if sub == "job-snapshot":
            return EXIT_OK, prepare_snapshot(cfg, args[0], stdin_read())
        if sub == "job-chat-result":
            return EXIT_OK, la.job_chat_result(args[0])
        if sub == "job-report":
            return EXIT_OK, la.job_report(args[0])
        if sub == "job-start":
            return EXIT_OK, la.job_start(args[0], stdin_read())
        if sub == "job-status":
            return EXIT_OK, la.job_status(args[0])
        if sub == "job-log":
            return EXIT_OK, la.job_log(args[0], int(args[1]))
        if sub == "job-stop":
            return EXIT_OK, la.job_stop(args[0])
        if sub == "job-retire":
            return EXIT_OK, la.job_retire(args[0])
        return EXIT_OK, la.job_list()
    except LauncherError as e:
        return e.code, e.as_json()
    except Exception as e:  # noqa: BLE001  内部の失敗も 1 行の JSON で返す（値は出さない）
        return EXIT_INTERNAL, {"ok": False, "error": {"class": "internal", "reason": "unexpected", "message": type(e).__name__}}


def main(argv: list[str] | None = None) -> int:
    code, obj = dispatch(sys.argv[1:] if argv is None else argv, dict(os.environ), lambda size=None: sys.stdin.read(1_000_000) if size is None else sys.stdin.buffer.read(size))
    if isinstance(obj, bytes):
        sys.stdout.buffer.write(obj)
        sys.stdout.buffer.flush()
    else:
        emit(obj)
    return code


if __name__ == "__main__":
    sys.exit(main())
