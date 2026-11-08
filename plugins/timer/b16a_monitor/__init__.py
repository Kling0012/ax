"""Stateful monitoring entry points for Tumu and environment audits."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Protocol


class EnvironmentJobs(Protocol):
    def run(self, *, requires: list[str], scope: str, entry: str) -> Mapping[str, Any]: ...


class Notifier(Protocol):
    def send(self, key: str, kind: str, target: str, detail: str) -> str:
        """Return sent, failed, or unknown; exceptions have unknown outcome."""
        ...


class Monitor:
    def __init__(self, database: str, *, tumu, environment_jobs: EnvironmentJobs,
                 notifier: Notifier, ledger):
        self.db = sqlite3.connect(database)
        self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()
        self.tumu, self.jobs, self.notifier, self.ledger = tumu, environment_jobs, notifier, ledger
        # A process may have died after invoking an external notification.
        rows = self.db.execute("SELECT key, value FROM state WHERE key LIKE 'notice:%'").fetchall()
        for key, value in rows:
            notice = json.loads(value)
            if notice['status'] == 'sending':
                notice['status'] = 'unknown'
                self._put(key, notice)

    def _get(self, key, default=None):
        row = self.db.execute('SELECT value FROM state WHERE key = ?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _put(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', (key, json.dumps(value)))

    def status(self, key):
        return self._get(key)

    def reconcile(self, key: str, status: str):
        if status not in ('sent', 'pending'):
            raise ValueError('invalid reconciliation status')
        notice = self._get('notice:' + key)
        if not notice or notice['status'] != 'unknown':
            raise ValueError('notice is not unknown')
        notice['status'] = status
        self._put('notice:' + key, notice)

    def _notice(self, key, kind, target, detail):
        skey = 'notice:' + key
        notice = self._get(skey, {'status': 'pending', 'reason': None})
        if notice['status'] in ('sent', 'sending', 'unknown'):
            return notice['status']
        notice['status'] = 'sending'
        self._put(skey, notice)
        try:
            result = self.notifier.send(key, kind, target, detail)
            if result not in ('sent', 'failed', 'unknown'):
                result = 'unknown'
            reason = None if result == 'sent' else result
        except Exception as exc:
            result, reason = 'unknown', type(exc).__name__
        notice = {'status': 'pending' if result == 'failed' else result, 'reason': reason}
        self._put(skey, notice)
        if result != 'sent':
            try:
                self.ledger.record(kind, target, key, reason)
            except Exception:
                pass
        return notice['status']

    def poll_tumu(self, target: str, *, mode: str = 'run'):
        if mode != 'run':
            return {'status': 'skipped', 'mode': mode}
        key = 'tumu:' + target
        state = self._get(key, {'failures': 0, 'episode': 0, 'status': 'unknown', 'stalled': False})
        try:
            observation = self.tumu.read(target)
            if not isinstance(observation, dict) or observation.get('status') not in ('ok', 'unreachable', 'error'):
                raise ValueError('invalid observation')
        except Exception:
            observation = {'status': 'error'}
        status = observation['status']
        if status == 'unreachable':
            if state['failures'] == 0:
                state['episode'] += 1
            state['failures'] += 1
            state['status'] = 'unreachable'
            self._put(key, state)
            if state['failures'] >= 2:
                self._notice(f"tumu:{target}:unreachable:{state['episode']}", 'tumu-unreachable', target, 'unreachable')
        elif status == 'ok':
            stalled = observation.get('stalled')
            if type(stalled) is not bool:
                state['status'] = 'error'
                self._put(key, state)
                return state
            state['failures'] = 0
            state['status'] = 'ok'
            if stalled and not state['stalled']:
                state['episode'] += 1
            state['stalled'] = stalled
            self._put(key, state)
            if stalled:
                self._notice(f"tumu:{target}:stalled:{state['episode']}", 'tumu-stalled', target, 'stalled')
        else:
            state['status'] = 'error'
            self._put(key, state)
        return state

    def poll_security(self, baselines: Mapping[str, Mapping[str, Any]], *, mode: str = 'run'):
        if mode != 'run':
            return {'status': 'skipped', 'mode': mode}
        try:
            results = self.jobs.run(requires=['security-audit'], scope='each', entry='audit-readonly')
            if not isinstance(results, Mapping):
                raise ValueError('invalid results')
        except Exception:
            results = {}
        output = {}
        for target, baseline in baselines.items():
            result = results.get(target)
            if (not isinstance(result, dict) or result.get('status') != 'ok'
                    or not isinstance(result.get('values'), dict)
                    or not isinstance(baseline, Mapping)
                    or not isinstance(baseline.get('values'), dict) or not baseline['values']
                    or not isinstance(baseline.get('version'), str) or not baseline['version']):
                output[target] = {'status': 'unavailable'}
                self._put('security:' + target, output[target])
                continue
            expected, actual = baseline['values'], result['values']
            # Missing measurements are not a clean comparison.
            if any(name not in actual for name in expected):
                output[target] = {'status': 'unavailable'}
                self._put('security:' + target, output[target])
                continue
            differences = sorted(name for name, value in expected.items() if actual[name] != value)
            output[target] = {'status': 'different' if differences else 'ok', 'differences': differences,
                              'baseline_version': baseline['version']}
            self._put('security:' + target, output[target])
            for difference in differences:
                # Encode the tuple to avoid collisions between names containing separators.
                ident = json.dumps([target, baseline['version'], difference], ensure_ascii=True, separators=(',', ':'))
                self._notice('security:' + ident, 'security-difference', target, difference)
        return output
