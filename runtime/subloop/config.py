"""環境変数から Services を組み立てる（本番の起動。D-6.1）。実在のホスト名・アドレスの値はここに書かない（compose の環境変数と profile から来る）。

環境変数（compose.d/sl-<id>.yaml が渡す。名前だけを列挙する）:
  AX_PLUGIN_ID      プラグイン ID（必須）
  AX_PLUGIN_DIR     manifest・graph・prompts の置き場（既定 /ax/plugins/subloop/<id>）
  AX_HOST           ホスト名（事象の source.host）
  AX_DATA_DIR       自分の保存先（既定 /data。subloop.db と outbox/）
  AX_WORK_ROOT      `work` のマウント先（既定 /work）
  AX_REMOTE_<名前>  git の remote 名 → URL かパス（例 AX_REMOTE_WORK_GIT。名前の非英数字は _ にする）
  AX_REPO_<名前>    プロジェクト名 → リポジトリ（例 AX_REPO_AX）
  AX_<部品>_URL     部品の URL の上書き（axcommon.client.service_url）
秘密（/run/secrets/ の下。値は読むだけ）: plugin-token-<id>（作業側の口の資格）、github-token（manifest の secrets にあるときだけ）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from axcommon import creds
from axcommon.client import ControlClient, LedgerClient, TimelineClient, WorkerPoolClient, ApiClient, service_url
from axcommon.defaults import Defaults
from axcommon.errors import ErrorRules
from axcommon.events import EventSink

from .ctx import Services
from .tools import GitHubClient, GitRunner, ShellRunner


def _prefixed(prefix: str, environ: dict) -> dict[str, str]:
    out = {}
    for k, v in environ.items():
        if k.startswith(prefix) and v:
            out[k[len(prefix):]] = v
    return out


def build_services(environ: dict | None = None, manifest: dict | None = None, *, repo_root: str | Path | None = None) -> Services:
    env = dict(os.environ if environ is None else environ)
    pid = env["AX_PLUGIN_ID"]
    root = Path(repo_root or env.get("AX_HOME_IN_CONTAINER") or "/ax")
    plugin_dir = Path(env.get("AX_PLUGIN_DIR") or root / "plugins" / "subloop" / pid)
    token = creds.read_secret_optional(f"plugin-token-{pid}")
    data_dir = Path(env.get("AX_DATA_DIR", "/data"))
    tl = TimelineClient(service_url("timeline", 8081, env), token)
    sink = EventSink(tl, pid, env.get("AX_HOST", "unknown"), data_dir / "outbox", project=(manifest or {}).get("project"), subloop=pid)
    gh_token = creds.read_secret_optional("github-token") if "github-token" in ((manifest or {}).get("secrets") or []) else None
    services = Services(
        plugin_id=pid, host=env.get("AX_HOST", "unknown"), sink=sink,
        pool=WorkerPoolClient(service_url("worker-pool", 8081, env), token),
        ledger=LedgerClient(service_url("ledger", 8081, env), token),
        control=ControlClient(service_url("control", 8081, env), token),
        timeline=tl, defaults=Defaults.load(), work_root=Path(env.get("AX_WORK_ROOT", "/work")),
        plugin_dir=plugin_dir, lib_dir=root / "plugins" / "subloop" / "_lib", repo_root=root,
        resources=ApiClient(service_url("resources", 8081, env), token),
        github=GitHubClient(gh_token) if gh_token else None,
        git=GitRunner(), shell=ShellRunner(), errors=ErrorRules.load(),
        remotes={re.sub(r"[^A-Z0-9]", "_", k.upper()): v for k, v in _prefixed("AX_REMOTE_", env).items()},
        repos={k.lower().replace("_", "-"): v for k, v in _prefixed("AX_REPO_", env).items()},
    )
    if pid == "release":
        try:
            from plugins.subloop.release.core.production import build_release_service
            from plugins.subloop.release.gate.adapters import GatewayReviews
            review_timeout = float(env.get("AX_PUBLISH_REVIEW_GRACE_S") or "60")
            if not 0 < review_timeout <= 86400:
                raise ValueError("invalid review timeout")
            gateway = ApiClient(env.get("AX_MODEL_GATEWAY") or "http://model-gateway-unavailable",
                                creds.read_secret_optional("model-gateway-key"), timeout=review_timeout)
            slots = ApiClient(env.get("AX_PUBLISH_SLOTS_URL") or "http://resources-review:8080",
                              creds.read_secret_optional("core-token-release-review"))
            reviews = GatewayReviews(services.resources, gateway, root / "config" / "roles.yaml", slots=slots)
            services.release_service = build_release_service(root, data_dir, services.host, sink,
                                                             repositories_path=env.get("AX_PUBLISH_REPOSITORIES_FILE"),
                                                             select_model=reviews.select, execute_model=reviews.execute,
                                                             products_path=env.get("AX_PRODUCTS_FILE"),
                                                             pii_path=env.get("AX_PII_LIST"),
                                                             hosts_path=env.get("AX_PUBLISH_HOSTS_FILE"),
                                                             terms_path=env.get("AX_PUBLISH_TERMS_FILE"))
        except Exception:
            import logging
            logging.getLogger("ax.subloop").error("release service initialization failed")
    return services


def remote_url(svc: Services, name: str) -> str | None:
    """remote 名（`work.git` など）を URL かパスに引く。そのままパス・URL の形ならそのまま返す。"""
    key = re.sub(r"[^A-Z0-9]", "_", name.upper())
    if key in svc.remotes:
        return svc.remotes[key]
    if name in svc.remotes:
        return svc.remotes[name]
    if "/" in name or "://" in name:
        return name
    return None


def repo_url(svc: Services, project: str) -> str | None:
    if project in svc.repos:
        return svc.repos[project]
    if "/" in project or "://" in project:
        return project
    return None
