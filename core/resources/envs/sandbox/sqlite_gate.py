"""SQLite-backed sandbox gate state and atomic token/slot transactions."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict

from .gate import GateDenied, GateService
from .probe_common import ProviderState


class SQLiteGate:
    def __init__(self, path, *, auth_digest, providers, clock=time.time, max_age_s=300,
                 max_budget_min=120, token_max_min=120):
        if not isinstance(auth_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", auth_digest):
            raise ValueError("invalid_auth_digest")
        self.path, self._auth_digest, self.providers = str(path), auth_digest, providers
        self.clock, self.max_age_s = clock, max_age_s
        self.max_budget_min, self.token_max_min = max_budget_min, token_max_min
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sandbox_gate_envs (
                    id TEXT PRIMARY KEY, state_json TEXT NOT NULL,
                    slot_job TEXT
                );
                CREATE TABLE IF NOT EXISTS sandbox_box_bindings (
                    box_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    PRIMARY KEY(box_id, job_id)
                );
                CREATE TABLE IF NOT EXISTS sandbox_remote_uncertain (
                    id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS sandbox_gate_tokens (
                    token_id TEXT PRIMARY KEY, env_id TEXT NOT NULL,
                    job_id TEXT NOT NULL, digest BLOB NOT NULL,
                    expires REAL NOT NULL, state TEXT NOT NULL,
                    FOREIGN KEY(env_id) REFERENCES sandbox_gate_envs(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS sandbox_gate_active_job
                    ON sandbox_gate_tokens(job_id) WHERE state = 'active';
            ''')
            columns = {row[1] for row in db.execute("PRAGMA table_info(sandbox_gate_tokens)")}
            if "capabilities_json" not in columns:
                db.execute("ALTER TABLE sandbox_gate_tokens ADD COLUMN capabilities_json TEXT")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def _authenticate(self, authorization):
        if not isinstance(authorization, str) or not re.fullmatch(r"Bearer [0-9a-f]{64}", authorization):
            raise GateDenied("unauthenticated")
        if not hmac.compare_digest(hashlib.sha256(authorization[7:].encode()).hexdigest(), self._auth_digest):
            raise GateDenied("unauthenticated")

    def save_state(self, state):
        if not isinstance(state, ProviderState) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", state.env_id):
            raise ValueError("invalid_environment")
        with self._transaction() as db:
            db.execute("INSERT INTO sandbox_gate_envs(id,state_json) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json", (state.env_id, json.dumps(asdict(state))))
            if state.gate_state == "excluded":
                db.execute("UPDATE sandbox_gate_tokens SET state='revoked' WHERE env_id=? AND state='active'", (state.env_id,))
                db.execute("UPDATE sandbox_gate_envs SET slot_job=NULL WHERE id=?", (state.env_id,))

    def bind_box(self, box_id, job_id):
        """Record a trusted assignment; never clears a quarantined box."""
        if (not isinstance(box_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,127}", box_id)
                or not isinstance(job_id, str) or not re.fullmatch(r"[a-z0-9-]{1,80}", job_id)):
            raise ValueError("invalid_box_binding")
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO sandbox_box_bindings(box_id,job_id) VALUES (?,?)", (box_id, job_id))

    def box_assigned(self, box_id, job_id):
        with self._db() as db:
            return db.execute("SELECT 1 FROM sandbox_box_bindings WHERE box_id=? AND job_id=?", (box_id, job_id)).fetchone() is not None

    def box_state(self, box_id):
        if self.remote_unknown(box_id):
            return {"local_state": "stopped", "remote_state": "unknown"}
        return None

    def mark_remote_unknown(self, env_id):
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO sandbox_remote_uncertain(id) VALUES (?)", (env_id,))

    def remote_unknown(self, env_id):
        with self._db() as db:
            return db.execute("SELECT 1 FROM sandbox_remote_uncertain WHERE id=?", (env_id,)).fetchone() is not None

    def clear_remote_unknown(self, env_id):
        """Explicit operator action after independent verification."""
        with self._transaction() as db:
            db.execute("DELETE FROM sandbox_remote_uncertain WHERE id=?", (env_id,))

    def get_state(self, env_id):
        with self._db() as db:
            row = db.execute("SELECT state_json FROM sandbox_gate_envs WHERE id=?", (env_id,)).fetchone()
        return ProviderState(**json.loads(row[0])) if row else None

    def _expire(self, db):
        expired = db.execute("SELECT token_id, env_id, job_id FROM sandbox_gate_tokens WHERE state='active' AND expires<=?", (self.clock(),)).fetchall()
        for row in expired:
            db.execute("UPDATE sandbox_gate_tokens SET state='expired' WHERE token_id=? AND state='active'", (row['token_id'],))
            db.execute("UPDATE sandbox_gate_envs SET slot_job=NULL WHERE id=? AND slot_job=?", (row['env_id'], row['job_id']))

    def issue(self, env_id, authorization, request):
        self._authenticate(authorization)
        with self._transaction() as db:
            self._expire(db)
            if db.execute("SELECT 1 FROM sandbox_remote_uncertain WHERE id=?", (env_id,)).fetchone():
                raise GateDenied("remote_state_unknown")
            row = db.execute("SELECT state_json FROM sandbox_gate_envs WHERE id=?", (env_id,)).fetchone()
            provider = self.providers.get(env_id)
            if row is None or provider is None:
                raise GateDenied("env_not_gateable")
            state = ProviderState(**json.loads(row[0]))
            def reserve(env, job):
                if db.execute("SELECT 1 FROM sandbox_gate_tokens WHERE job_id=? AND state='active'", (job,)).fetchone():
                    raise GateDenied("token_exists")
                return db.execute("UPDATE sandbox_gate_envs SET slot_job=? WHERE id=? AND slot_job IS NULL", (job, env)).rowcount == 1
            def release(env, job):
                db.execute("UPDATE sandbox_gate_envs SET slot_job=NULL WHERE id=? AND slot_job=?", (env, job))
            service = GateService(auth_digest=self._auth_digest, state=state, capabilities=provider.capabilities,
                                  read_worker_key=provider.read_worker_key, measure=provider.measure,
                                  reserve=reserve, release=release, clock=self.clock, max_age_s=self.max_age_s,
                                  max_budget_min=self.max_budget_min, token_max_min=self.token_max_min)
            try:
                issued = service.issue(authorization, request)
            except GateDenied as exc:
                if exc.reason == "worker_key_missing":
                    db.execute("UPDATE sandbox_gate_envs SET state_json=?, slot_job=NULL WHERE id=?", (json.dumps(asdict(state)), env_id))
                    db.execute("UPDATE sandbox_gate_tokens SET state='revoked' WHERE env_id=? AND state='active'", (env_id,))
                    # Commit the exclusion while still rejecting the request.
                    db.commit()
                raise
            db.execute("INSERT INTO sandbox_gate_tokens(token_id,env_id,job_id,digest,expires,state,capabilities_json) VALUES(?,?,?,?,?,'active',?)",
                       (issued.token_id, env_id, request['job_id'], hashlib.sha256(issued.token.encode()).digest(), issued.expires_at, json.dumps(request['tools'])))
            return issued

    def revoke(self, env_id, token_id, authorization):
        self._authenticate(authorization)
        with self._transaction() as db:
            self._expire(db)
            row = db.execute("SELECT job_id,state FROM sandbox_gate_tokens WHERE token_id=? AND env_id=?", (token_id, env_id)).fetchone()
            if row is None:
                raise GateDenied("token_not_found")
            if row['state'] != 'active':
                return False
            db.execute("UPDATE sandbox_gate_tokens SET state='revoked' WHERE token_id=? AND state='active'", (token_id,))
            db.execute("UPDATE sandbox_gate_envs SET slot_job=NULL WHERE id=? AND slot_job=?", (env_id, row['job_id']))
            return True

    def token_context(self, env_id, token):
        """Resolve an active token to its job and immutable capability grant."""
        if not isinstance(token, str) or not re.fullmatch(r"axg-[0-9a-f]{64}", token):
            return None
        digest = hashlib.sha256(token.encode()).digest()
        with self._transaction() as db:
            self._expire(db)
            if db.execute("SELECT 1 FROM sandbox_remote_uncertain WHERE id=?", (env_id,)).fetchone():
                return None
            rows = db.execute("""SELECT t.digest,t.job_id,t.capabilities_json,e.state_json,e.slot_job
                FROM sandbox_gate_tokens t JOIN sandbox_gate_envs e ON e.id=t.env_id
                WHERE t.env_id=? AND t.state='active'""", (env_id,)).fetchall()
            for row in rows:
                if not hmac.compare_digest(digest, row['digest']):
                    continue
                if json.loads(row['state_json'])['gate_state'] not in ('trial', 'available') or row['slot_job'] != row['job_id']:
                    return None
                try:
                    capabilities = json.loads(row['capabilities_json'])
                except (TypeError, ValueError):
                    return None
                if not isinstance(capabilities, list) or any(not isinstance(x, str) for x in capabilities):
                    return None
                return {'job_id': row['job_id'], 'capabilities': frozenset(capabilities)}
            return None

    def validate(self, env_id, token):
        if not isinstance(token, str) or not re.fullmatch(r"axg-[0-9a-f]{64}", token):
            return False
        digest = hashlib.sha256(token.encode()).digest()
        with self._transaction() as db:
            self._expire(db)
            rows = db.execute("SELECT t.digest,e.state_json FROM sandbox_gate_tokens t JOIN sandbox_gate_envs e ON e.id=t.env_id WHERE t.env_id=? AND t.state='active'", (env_id,)).fetchall()
            return any(json.loads(row['state_json'])['gate_state'] in ('trial', 'available') and hmac.compare_digest(digest, row['digest']) for row in rows)
