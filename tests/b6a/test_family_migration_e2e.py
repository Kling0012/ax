"""Public pool API migration from a portable, pre-family SQLite fixture."""
import importlib
import sqlite3

from starlette.testclient import TestClient

from tests.b6a.test_dispatch_e2e import Control, ResourceAdapter, system


# Frozen subset of the pre-family pool schema. Defaults and existing columns
# represent an old database; no current migration code is used to create it.
OLD_SCHEMA = """
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, role TEXT NOT NULL, prompt TEXT NOT NULL DEFAULT '',
 sensitivity TEXT NOT NULL DEFAULT 'public', needs_vision INTEGER NOT NULL DEFAULT 0,
 project TEXT, subloop TEXT, item_id TEXT, todo_id TEXT, state TEXT NOT NULL DEFAULT 'queued',
 model TEXT, workdir_id TEXT, session_id TEXT, resume_from TEXT,
 resume_required INTEGER NOT NULL DEFAULT 0, root TEXT, chain_length INTEGER NOT NULL DEFAULT 0,
 last_context INTEGER, placement TEXT NOT NULL DEFAULT 'local', tools TEXT NOT NULL DEFAULT '[]',
 budget_min INTEGER, deadline TEXT, reason TEXT NOT NULL DEFAULT '', verify TEXT NOT NULL DEFAULT '',
 attachments TEXT NOT NULL DEFAULT '[]', depends TEXT NOT NULL DEFAULT '[]', request TEXT NOT NULL DEFAULT '',
 cancel_requested INTEGER NOT NULL DEFAULT 0, slot_released INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, ended_at TEXT,
 prefix_id TEXT NOT NULL DEFAULT '', report_status TEXT, report_text TEXT NOT NULL DEFAULT '',
 human_action TEXT NOT NULL DEFAULT '', verify_rc INTEGER, verify_log_tail TEXT NOT NULL DEFAULT '',
 changed_files TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_no INTEGER NOT NULL, job_id TEXT NOT NULL,
 model TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'running', started_at TEXT NOT NULL,
 ended_at TEXT, result TEXT NOT NULL DEFAULT '', verify_rc INTEGER, counts_as_failure TEXT,
 switch_reason TEXT NOT NULL DEFAULT '', usage TEXT NOT NULL DEFAULT '{}'
);
"""


def test_old_database_migration_preserves_jobs_and_reservation(system, tmp_path):
    _, resources, _ = system
    db, backup = tmp_path / 'old.sqlite', tmp_path / 'backup.sqlite'
    jid = 'job-legacy'
    with sqlite3.connect(db) as old:
        old.executescript(OLD_SCHEMA)
        old.execute("INSERT INTO jobs(id,role,prompt,state,model,created_at) VALUES (?,?,?,?,?,?)",
                    (jid, 'summary', 'legacy running', 'running', 'example/model', '2026-01-01T00:00:00Z'))
        old.execute("INSERT INTO attempts(attempt_no,job_id,model,started_at) VALUES (?,?,?,?)",
                    (1, jid, 'example/model', '2026-01-01T00:00:00Z'))
        old.commit()
        with sqlite3.connect(backup) as snapshot:
            old.backup(snapshot)
    adapter = ResourceAdapter(resources)
    adapter.post('/v1/slots/reserve', {'reservation_id': jid, 'model': 'example/model'})
    pool = importlib.import_module('plugins.resource.worker-pool.app')
    core, _, service = pool.build(pool.Config(db=str(db)), resources=adapter, control=Control(),
                                  run_heartbeat_loop=False, emit=lambda *a, **k: None)
    client = TestClient(core, headers={'Authorization': 'Bearer test-intake'})
    result = client.get('/v1/jobs/' + jid)
    assert result.status_code == 200
    assert result.json()['model'] == 'example/model' and result.json()['family'] is None
    body = {'role': 'second_opinion', 'prompt': 'review', 'sensitivity': 'public'}
    assert client.post('/v1/jobs', json={**body, 'prior_job_id': jid}).status_code == 422
    assert client.post('/v1/jobs', json={**body, 'pin_model': 'example/model'}).status_code == 400
    assert client.post('/v1/jobs', json=body).status_code == 202
    assert resources.get('/v1/slots').json()['models']['example/model']['used'] == 1
    service.store.close()
    # The old DB remains readable through the SQLite interface and its backup
    # retains the original schema. This does not claim full old-code rollback.
    with sqlite3.connect(db) as migrated, sqlite3.connect(backup) as original:
        assert migrated.execute('SELECT model FROM jobs WHERE id=?', (jid,)).fetchone() == ('example/model',)
        row = migrated.execute('SELECT family, launched FROM attempts WHERE job_id=?', (jid,)).fetchone()
        assert row == (None, 0)
        assert original.execute('SELECT model FROM jobs WHERE id=?', (jid,)).fetchone() == ('example/model',)
        assert 'family' not in {r[1] for r in original.execute('PRAGMA table_info(attempts)')}
