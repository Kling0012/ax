"""Exercise queued cancellation through the worker-pool HTTP interface."""
import importlib

from starlette.testclient import TestClient


def test_cancel_queued_job_preserves_other_jobs_and_rejects_unknown(tmp_path, monkeypatch):
    from axcommon.creds import sha256_hex
    table = tmp_path / "pool-core-tokens"
    table.write_text("intake " + sha256_hex("test-intake") + "\n")
    monkeypatch.setenv("AX_CORE_TOKENS", str(table))
    pool = importlib.import_module("plugins.resource.worker-pool.app")
    app, _, _ = pool.build(pool.Config(db=str(tmp_path / "jobs.sqlite")), run_heartbeat_loop=False)
    with TestClient(app, headers={"Authorization": "Bearer test-intake"}) as client:
        body = {"role": "research", "prompt": "inspect", "sensitivity": "private"}
        first = client.post("/v1/jobs", json=body)
        second = client.post("/v1/jobs", json=body)
        assert first.status_code == second.status_code == 202
        target = first.json()["job_id"]
        other = second.json()["job_id"]
        result = client.post(f"/v1/jobs/{target}/cancel", json={})
        assert result.status_code == 200
        assert result.json()["job"]["state"] == "cancelled"
        assert client.get(f"/v1/jobs/{target}").json()["state"] == "cancelled"
        assert client.get(f"/v1/jobs/{other}").json()["state"] == "queued"
        assert client.post("/v1/jobs/nonexistent/cancel", json={}).status_code == 404
