"""資源台帳 resources のアプリの組み立て。設定は環境変数から読む。

  AX_HOST                   事象の source.host に入れるホスト名（profile の host）
  AX_RESOURCES_DB           既定 /data/resources.sqlite
  AX_MODELS_FILE            登録簿の実ファイル（S-16.3 の models_file）
  AX_CORE_TOKENS_SHA256     core-tokens.sha256 の置き場（既定 /state/core-tokens.sha256）
  AX_MODEL_GATEWAY          ゲートウェイの URL
  AX_JEV_URL                Jev の接続先（既定 https://api.typesafe.ai/v1/systemone）
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from axcommon import creds
from axcommon.http import create_app

from . import COMPONENT, VERSION, api, discovery
from .catalog import load_models_file, models_path
from .core_auth import CoreAuth
from .store import Store

log = logging.getLogger("resources")


@dataclass
class Config:
    host: str = "unknown"
    db: str = "/data/resources.sqlite"
    models_file: str | None = None
    core_tokens_sha256: str | None = None
    model_gateway: str | None = None
    jev_url: str | None = None
    ext_dir: str | None = None
    worker_pool_db: str | None = None

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        e = os.environ if env is None else env
        return cls(
            host=e.get("AX_HOST", "unknown"),
            db=e.get("AX_RESOURCES_DB", cls.db),
            models_file=e.get("AX_MODELS_FILE") or None,
            core_tokens_sha256=e.get("AX_CORE_TOKENS_SHA256") or None,
            model_gateway=e.get("AX_MODEL_GATEWAY") or None,
            jev_url=e.get("AX_JEV_URL") or None,
            ext_dir=e.get("AX_RESOURCES_EXT_DIR") or None,
            worker_pool_db=e.get("AX_WORKER_POOL_LOCK_DB") or None,
        )


def build(cfg: Config | None = None, *, store: Store | None = None, auth: CoreAuth | None = None,
          run_heartbeat_loop: bool = True, env_jobs=None, b18_gate=None, b18_mcp=None):
    """(中核の口のアプリ, 作業側の口のアプリ, Service) を返す。"""
    cfg = cfg or Config.from_env()
    store = store or Store(cfg.db)
    store.worker_pool_db = cfg.worker_pool_db
    if auth is None:
        path = cfg.core_tokens_sha256 or "/state/core-tokens.sha256"
        auth = CoreAuth(path) if Path(path).is_file() else CoreAuth(creds.TokenTable())
    # 初回だけ登録簿を移す（D-8）。失敗しても起動は止めず、その理由を health に出す
    seed_error: str | None = None
    seeded = 0
    try:
        seeded = store.seed(load_models_file(cfg.models_file or models_path()))
    except Exception as e:  # noqa: BLE001  登録簿が読めなくても他は動く（fail-soft）
        seed_error = type(e).__name__
        log.warning("登録簿の移行に失敗: %s", seed_error)
    svc = api.Service(store=store, auth=auth, host=cfg.host, model_gateway=cfg.model_gateway, jev_url=cfg.jev_url)

    b18_configured = b18_gate is not None or b18_mcp is not None
    b18_status = "shared_capacity_unavailable" if b18_configured else "not_configured"
    if b18_configured:
        log.warning("sandbox disabled: shared capacity unavailable")

    def health() -> dict:
        return {"seeded": seeded, "seed_error": seed_error, "b18_status": b18_status, **auth.health()}

    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def sandbox_unavailable(request):
        return JSONResponse({"error": {"code": "shared_capacity_unavailable"}}, status_code=503)

    gate_routes = [Route("/v1/envs/{env_id}/gate-token", sandbox_unavailable, methods=["POST"]),
                   Route("/v1/envs/{env_id}/gate-token/{token_id}/revoke", sandbox_unavailable, methods=["POST"])] if b18_configured else []
    mcp_routes = [Route("/mcp/{env_id}", sandbox_unavailable, methods=["POST"])] if b18_configured else []

    def sandbox_adapter_for(driver, box_id):
        raise ValueError("sandbox shared capacity unavailable")

    def sandbox_runner_for(executable, box_id, *, timeout=600, cancel_grace=2):
        raise ValueError("sandbox shared capacity unavailable")

    svc.sandbox_adapter_for = sandbox_adapter_for
    svc.sandbox_runner_for = sandbox_runner_for

    async def start_discovery():
        if not cfg.model_gateway:
            return

        async def run():
            try:
                await asyncio.to_thread(discovery.reconcile, store, base_url=cfg.model_gateway)
            except Exception as exc:  # discovery failures must not stop resources
                log.warning("model discovery failed: %s", type(exc).__name__)
                try:
                    await asyncio.to_thread(store.record_discovery, ok=False, error="discovery_failed")
                except Exception as record_exc:
                    log.warning("model discovery record failed: %s", type(record_exc).__name__)

        svc.discovery_task = asyncio.create_task(run())

    async def stop_discovery():
        task = getattr(svc, "discovery_task", None)
        if task is not None:
            await task

    core = create_app(
        COMPONENT, VERSION,
        routes=api.build_routes(svc, work_side=False) + gate_routes,
        sink=svc.sink,
        extra_health=health,
        ext_dir=cfg.ext_dir or str(Path(__file__).parent / "ext"),
        on_startup=[svc.bind_loop, start_discovery],
        on_shutdown=[stop_discovery],
        run_heartbeat_loop=run_heartbeat_loop,
    )

    from .envs.jobs import Jobs
    from .envs.adapters.ssh_host import RestrictedJobExecutor
    env_jobs = env_jobs if env_jobs is not None else Jobs(executor=RestrictedJobExecutor())
    env_jobs.bind(store)

    def work_health() -> dict:
        return {"resources_open": True, "b18_status": b18_status}

    work = create_app(
        COMPONENT, VERSION,
        routes=api.build_routes(svc, work_side=True) + env_jobs.routes() + mcp_routes,
        sink=None,
        extra_health=work_health,
        on_startup=[env_jobs.start],
        on_shutdown=[env_jobs.stop],
        run_heartbeat_loop=False,
    )
    # Resolve each secret request against the current validated resource registry.
    # Missing configuration only rejects that request, leaving resources running.
    def secret_adapter_for(environment_id: str):
        from .envs.adapters.base import AdapterError
        from .envs.adapters.ssh_host import SSHHostAdapter
        from .envs.registry import load_environment_file

        try:
            entry = load_environment_file().get(environment_id)
            if entry is None:
                raise AdapterError("environment unavailable")
            return SSHHostAdapter.from_environment(
                entry, secrets_dir=os.environ.get("AX_SECRETS_PATH", "/run/secrets")
            )
        except AdapterError:
            raise
        except Exception:
            raise AdapterError("environment configuration unavailable") from None

    def secret_push(slot: str):
        import re
        import stat
        import yaml
        from .envs.registry import load_environment_file

        if not isinstance(slot, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}", slot):
            raise ValueError("invalid slot")
        config = Path(os.environ.get("AX_CONFIG_DIR", "/ax/config"))
        definitions = yaml.safe_load((config / "secret-slots.yaml").read_text(encoding="utf-8"))["slots"]
        definition = definitions[slot]
        target = definition["target"]
        if (definition.get("file", slot) != slot or "write" not in definition.get("operations", [])
                or not isinstance(target, dict) or set(target) != {"env", "name"}):
            raise ValueError("invalid slot")
        environment = load_environment_file().get(target["env"])
        if not environment:
            raise ValueError("environment unavailable")
        targets = [x for x in environment.get("secret_targets", []) if x.get("name") == target["name"]
                   and x.get("slot", slot) == slot]
        if len(targets) != 1:
            raise ValueError("target unavailable")
        limit = targets[0].get("max_bytes")
        if type(limit) is not int or not 1 <= limit <= 65536:
            raise ValueError("invalid limit")
        root = os.environ.get("AX_HQ_SLOTS_DIR", "/secrets-in")
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(slot, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limit:
                    raise ValueError("invalid slot file")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    value = stream.read(limit + 1)
                if not 0 < len(value) <= limit:
                    raise ValueError("invalid slot size")
            finally:
                os.close(fd)
        finally:
            os.close(directory)
        secret_adapter_for(target["env"]).secret_put(target["name"], value)

    svc.secret_push = secret_push
    svc.secret_adapter_for = secret_adapter_for
    core.state.secret_adapter_for = secret_adapter_for
    core.state.service = svc
    work.state.env_jobs = env_jobs
    work.state.service = svc
    return core, work, svc
