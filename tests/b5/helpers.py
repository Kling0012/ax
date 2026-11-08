"""tests/b5 の補助。登録簿を tmp に置いて入口のアプリを作る。ネットワークは FakeTransport だけ。"""
from __future__ import annotations

from pathlib import Path

from axcommon.events import EventSink

from core.intake.app import build_app
from core.intake.vias import Ident

LOCAL = Ident("local", "commander", "local-operator", "operate")
HQ = Ident("hq", "hq", "user", "irreversible")


def write_registry(config_dir: Path, entries: list[dict], drop: list[dict] | None = None) -> Path:
    import yaml

    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "intake-destinations.yaml").write_text(
        yaml.safe_dump(entries, allow_unicode=True, sort_keys=False), encoding="utf-8")
    d = config_dir / "intake-destinations.d"
    d.mkdir(exist_ok=True)
    for i, e in enumerate(drop or []):
        (d / f"drop{i}.yaml").write_text(yaml.safe_dump(e, allow_unicode=True), encoding="utf-8")
    return config_dir


def simple_dest(name: str = "demo.echo", **over) -> dict:
    d = {
        "name": name,
        "title": "試験用",
        "schema": "healthz",  # schemas/ の既存の Schema（ok/component/version が要る）
        "handler": "http://demo:8080/v1/echo",
        "allowed_from": ["commander", "hq"],
        "via": ["local", "ssh", "tailnet", "hq"],
        "tier": "read",
        "ack": "sync",
    }
    d.update(over)
    return d


def make_app(tmp_path: Path, transport, entries: list[dict] | None = None, *, sink_transport=None,
             dest_dir=None, audit_path=None, idem_path=None):
    from axcommon.client import TimelineClient

    entries = entries if entries is not None else [simple_dest()]
    write_registry(tmp_path / "config", entries)
    sink = EventSink(TimelineClient("http://timeline:8080", transport=sink_transport or transport),
                     "intake", "test-host", tmp_path / "outbox")
    from core.intake.idempotency import IdempotencyStore

    idem = IdempotencyStore(idem_path or (tmp_path / "idem.jsonl"), retain_h=24.0)
    from core.intake.audit import AccessLog

    app = build_app(config_dir=tmp_path / "config", transport=transport, sink=sink, idem=idem,
                    audit=AccessLog(audit_path) if audit_path else None,
                    dest_dir=dest_dir if dest_dir is not None else str(Path(__file__).resolve().parents[2] / "core" / "intake" / "dest"),
                    run_heartbeat_loop=False)
    return app


def with_ident(app, ident: Ident):
    """ASGI の入れ子。受け口（httpsock）が scope に置くのと同じ物を置く。"""
    from starlette.testclient import TestClient

    async def wrap(scope, receive, send):
        if scope["type"] == "http":
            scope["ax_ident"] = ident
        await app(scope, receive, send)

    return TestClient(wrap)


def client_for(app, ident: Ident = LOCAL):
    return with_ident(app, ident)


def events_of(transport, kind: str | None = None) -> list[dict]:
    out = []
    for c in transport.calls:
        if c.path != "/v1/events" or c.method != "POST":
            continue
        body = c.json
        for ev in body if isinstance(body, list) else [body]:
            if kind is None or ev.get("kind") == kind:
                out.append(ev)
    return out


def instruction(destination: str, payload: dict | None = None, **over) -> dict:
    d = {"from": "commander", "destination": destination, "payload": payload or {}}
    d.update(over)
    return d
