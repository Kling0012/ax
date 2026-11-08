"""Dispatch queued jobs and reconcile launcher outcomes against the resource ledger."""
from __future__ import annotations

import logging
import json
import re
import os
import fcntl
import hashlib
import threading
from urllib.parse import quote

from axcommon.client import ApiError, TimelineClient, service_url
from .prompt import PromptParts, build, arrangement_for, job_block
from plugins.subloop.release.lesson import LessonService, timeline_events
from axcommon import timeutil
from .launcher_client import LauncherError
from .selection import select, supports_runner

log = logging.getLogger('worker-pool.dispatch')
LEVELS = {'assist': 'L2', 'test_design': 'L3', 'impl': 'L3', 'impl_public': 'L3',
          'review': 'L3', 'manager': 'L1', 'impl_hard': 'L4', 'review_final': 'L4',
          'backup_manager': 'L4'}


class Dispatcher:
    def __init__(self, service, launcher):
        self.svc, self.launcher = service, launcher
        self.timeline = TimelineClient(service_url("timeline"))
        self._tick_lock = threading.Lock()
        self._outcome_unavailable = False

    def release(self, job):
        self.svc.resources.post('/v1/slots/release', {'reservation_id': job['id']})
        with self.svc.store._lock:
            self.svc.store._conn.execute('UPDATE jobs SET slot_released=1 WHERE id=?', (job['id'],))

    def read_log(self, job_id):
        try:
            text = self.launcher.job_log(job_id)['log']
            if not isinstance(text, str):
                raise ValueError('invalid log')
            return text
        except Exception:
            return None

    @staticmethod
    def report(text):
        if text is None:
            return None, ''
        # Structured CLI output is authoritative only when its final record is
        # a successful result. Never interpret diagnostic or event text as a report.
        raw_lines = [line for line in text.splitlines() if line.strip()]
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            try:
                data = json.loads(raw_lines[-1]) if raw_lines else None
            except (ValueError, TypeError):
                data = None
        if data is not None:
            if (not isinstance(data, dict) or data.get('is_error') is not False
                    or data.get('type', 'result') != 'result'
                    or data.get('subtype', 'success') != 'success'
                    or not isinstance(data.get('result'), str)):
                return None, ''
            text = data['result']
        elif any(line.lstrip().startswith(('{', '[cli:')) for line in raw_lines):
            return None, ''
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return None, ''
        human = ''
        if lines[-1].startswith('HUMAN_ACTION:'):
            human = lines.pop().partition(':')[2].strip()
        if not lines:
            return None, ''
        m = re.fullmatch(r'STATUS:\s*(ready|blocked|failed|dropped)', lines[-1])
        if not m or (human and m.group(1) != 'blocked'):
            return None, ''
        return m.group(1), human

    def finish(self, job, state, reason, *, verify_rc=None, verify_log=''):
        self.svc.store.record_result(job['id'], status=job.get('report_status') or 'failed',
            text=job.get('report_text') or '', human_action=job.get('human_action') or '',
            verify_rc=verify_rc, verify_log_tail=verify_log)
        attempts = self.svc.store.attempts(job['id'])
        if attempts and attempts[-1]['state'] == 'running':
            self.svc.store.finish_attempt(attempts[-1]['id'], state=state, result=reason, verify_rc=verify_rc)
        self.svc.store.set_state(job['id'], state, reason=reason)
        self.svc.emit("worker.attempt", job, {"job_id": job["id"], "state": state, "reason": reason})
        self.svc.emit("job.state", job, {"job_id": job["id"], "state": state, "reason": reason})
        self.release(job)

    def send_outcome(self, job):
        # The attempt is the durable outbox; terminal jobs remain eligible until acknowledged.
        if self._outcome_unavailable:
            return
        for attempt in self.svc.store.attempts(job['id']):
            if attempt['outcome_sent'] or attempt['state'] == 'running' or attempt['level_generation'] is None:
                continue
            passed = (attempt['state'] == 'ok' and job.get('report_status') == 'ready'
                      and type(attempt['verify_rc']) is int and attempt['verify_rc'] == 0)
            knobs = self.svc.selection_knobs()
            body = {'job_id': job['id'], 'trial_id': f"{job['id']}:{attempt['id']}",
                    'outcome': 'pass' if passed else 'fail', 'verify_rc': attempt['verify_rc'],
                    'counts_as_failure': 'no' if passed else 'hold',
                    'level_generation': attempt['level_generation'],
                    'promote_after': knobs.promote_after, 'demote_after': knobs.demote_after}
            try:
                self.svc.resources.post('/v1/resources/' + quote(attempt['model'], safe='/') + '/outcome', body)
            except Exception as exc:
                log.warning('outcome delivery unavailable: %s', type(exc).__name__)
                self._outcome_unavailable = True
                return
            with self.svc.store._lock:
                self.svc.store._conn.execute('UPDATE attempts SET outcome_sent=1 WHERE id=?', (attempt['id'],))

    def start_verification(self, job):
        verify_id = 'verify-' + job['id']
        request = {'job_id': verify_id, 'runner': os.environ.get('AX_WORKER_POOL_VERIFY_RUNNER', 'verify-python'),
                   'allow_hosts': [], 'budget_min': job['budget_min'] or 30, 'verify': job['verify'],
                   'prompt': 'Run the requested verification.', 'workdir_id': job['workdir_id'] or job['id']}
        if job['verify'] == 'ax:research-report-v1':
            read_input = (job.get('request') or {}).get('read_input') or {}
            revision = read_input.get('revision', '')
            if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', revision):
                self.finish(job, 'failed', 'verification_unavailable')
                return
            request['runner'] = 'verify-python'
            request['snapshot_id'] = job['id']
            request['verify'] = 'python /opt/ax/read_verify.py --revision ' + revision
        # Save the intent first; never repeat a start whose result is uncertain.
        self.svc.store.set_state(job['id'], 'verifying', reason='verification_started')
        try:
            self.launcher.job_start(request)
        except LauncherError as exc:
            if exc.exit_code in (2, 3, 4, 5) and exc.reason not in ('empty_response', 'bad_response'):
                self.finish(job, 'failed', 'verification_unavailable')
            else:
                raise

    def reconcile(self, job):
        store = self.svc.store
        if job['state'] in ('ok', 'failed', 'timed_out', 'cancelled', 'lost', 'rejected'):
            if job['model'] and not job['slot_released']:
                self.release(job)
            self.send_outcome(job)
            return
        if job['state'] == 'verifying':
            verify_id = 'verify-' + job['id']
            status = self.launcher.job_stop(verify_id) if job['cancel_requested'] else self.launcher.job_status(verify_id)
            if status.get('state') != 'exited' or type(status.get('exit_code')) is not int:
                return
            rc = status['exit_code']
            log_text = self.read_log(verify_id) or ''
            state = 'cancelled' if job['cancel_requested'] else ('ok' if rc == 0 and job['report_status'] == 'ready' else 'failed')
            reason = 'cancelled' if job['cancel_requested'] else ('verified' if state == 'ok' else 'verification_failed')
            if state == 'ok' and job['verify'] == 'ax:research-report-v1':
                try:
                    hashes = re.findall(r'(?m)^research-report-sha256: ([0-9a-f]{64})$', log_text)
                    artifact = self.launcher.job_report(job['id'])
                    text = artifact['report']
                    if (artifact.get('job_id') != job['id'] or not isinstance(text, str) or len(hashes) != 1
                            or hashlib.sha256(text.encode('utf-8')).hexdigest() != hashes[0]):
                        raise ValueError('report changed')
                    store.record_result(job['id'], status='ready', text=text, human_action='')
                    job = store.get(job['id'])
                except (LauncherError, ValueError, KeyError, TypeError):
                    state, reason = 'failed', 'verified_report_unavailable'
            self.finish(job, state, reason, verify_rc=rc, verify_log=log_text)
            return
        if job['state'] not in ('launching', 'submitted', 'running'):
            return
        try:
            status = self.launcher.job_stop(job['id']) if job['cancel_requested'] else self.launcher.job_status(job['id'])
        except LauncherError as exc:
            # launcher 側に記録が無い（rc=3）。launching/submitted のまま参照だけが残った仕事は
            # lost として確定させ、通知票の空回しを止める（job-start は次の queued からやり直す）。
            if exc.exit_code == 3 and job['state'] in ('launching', 'submitted'):
                self.finish(job, 'lost', 'launcher_job_not_found')
                return
            raise
        if (not job['cancel_requested'] and status.get('job_id') == job['id']
                and status.get('state') == 'gone' and 'exit_code' in status
                and status['exit_code'] is None):
            self.finish(job, 'lost', 'launcher_job_not_found')
            return
        if status.get('state') == 'exited' and type(status.get('exit_code')) is int:
            attempts = store.attempts(job['id'])
            runner = attempts[-1].get('runner') if attempts else None
            chat_result = None
            if runner == 'chat-json':
                try:
                    chat_result = self.launcher.job_chat_result(job['id'])
                    text = json.dumps(chat_result, ensure_ascii=False, separators=(',', ':'))
                except (LauncherError, ValueError, TypeError, AttributeError):
                    self.finish(job, 'failed', 'chat_result_unavailable')
                    return
            else:
                text = self.read_log(job['id'])
            report_status, human = self.report(text)
            fixed_profiles = {
                'ax:change-safety-review-v1': ('second_opinion', 'ext/b14/safety/review'),
                'ax:change-injection-screen-v1': ('screen', 'ext/b14/safety/screen'),
            }
            profile = fixed_profiles.get(job['verify'])
            if profile:
                expected_evidence = {'job_id': job['id'], 'runner': 'chat-json', 'profile': 'tool-free-chat-v1',
                                     'request_tools_absent': True, 'inspect_verified': True,
                                     'trust_basis': 'host_managed_runner_and_image'}
                if (runner != 'chat-json' or not isinstance(chat_result, dict)
                        or chat_result.get('job_id') != job['id'] or chat_result.get('tool_free') is not True
                        or chat_result.get('tool_free_evidence') != expected_evidence
                        or chat_result['tool_free_evidence'].get('request_tools_absent') is not True
                        or chat_result['tool_free_evidence'].get('inspect_verified') is not True):
                    self.finish(job, 'failed', 'tool_isolation_unverified')
                    return
                with store._lock:
                    store._conn.execute("UPDATE jobs SET tool_free=1,tool_free_evidence=? WHERE id=?",
                                        (json.dumps(expected_evidence, separators=(',', ':')), job['id']))
                if job['cancel_requested']:
                    self.finish(job, 'cancelled', 'cancelled')
                    return
                if status['exit_code'] != 0 or job['role'] != profile[0]:
                    self.finish(job, 'failed', 'verification_unavailable')
                    return
                try:
                    from axcommon.schemas import load
                    from jsonschema import Draft202012Validator
                    envelope = json.loads(text)
                    if (isinstance(envelope, dict) and envelope.get('type') == 'result'
                            and envelope.get('subtype') == 'success' and envelope.get('is_error') is False
                            and isinstance(envelope.get('result'), str)):
                        value = json.loads(envelope['result'])
                    elif (isinstance(envelope, dict) and envelope.get('ok') is True
                          and type(envelope.get('exit_code')) is int and envelope['exit_code'] == 0
                          and 'json' in envelope):
                        value = envelope['json']
                    else:
                        raise ValueError('invalid result envelope')
                    valid = Draft202012Validator(load(profile[1])).is_valid(value)
                except Exception:
                    self.finish(job, 'failed', 'verification_unavailable')
                    return
                store.record_result(job['id'], status='ready' if valid else 'failed',
                                    text=json.dumps(value, ensure_ascii=False, separators=(',', ':')))
                self.finish(store.get(job['id']), 'ok' if valid else 'failed',
                            'verified' if valid else 'verification_failed', verify_rc=0 if valid else 1)
                return
            # A plain manager response may be a JSON object followed by its status,
            # rather than a CLI envelope. Require the entire payload to be JSON.
            if report_status is None and job['role'] == 'manager' and job['verify'] == 'ax:manager-decision-v1' and isinstance(text, str):
                lines = text.strip().splitlines()
                if len(lines) > 1 and lines[-1].strip() == 'STATUS: ready':
                    try:
                        obj = json.loads('\n'.join(lines[:-1]))
                        if isinstance(obj, dict) and 'actions' in obj:
                            report_status = 'ready'
                    except ValueError:
                        pass
                else:
                    # Manager returns a JSON decision rather than a STATUS line.
                    # Only a successful result envelope may take this route; the
                    # fixed manager-decision schema still decides success below.
                    try:
                        try:
                            envelope = json.loads(text)
                        except ValueError:
                            envelope = json.loads(lines[-1])
                        if (isinstance(envelope, dict) and envelope.get('type') == 'result'
                                and envelope.get('subtype', 'success') == 'success'
                                and envelope.get('is_error') is False
                                and isinstance(envelope.get('result'), str)):
                            obj = json.loads(envelope['result'])
                            if isinstance(obj, dict) and 'actions' in obj:
                                report_status = 'ready'
                    except (ValueError, IndexError):
                        pass
            store.record_result(job['id'], status=report_status or 'failed', text=text or '', human_action=human)
            if isinstance(status.get('wd'), str) and re.fullmatch(r'[a-z0-9-]{1,40}', status['wd']):
                with store._lock:
                    store._conn.execute('UPDATE jobs SET workdir_id=? WHERE id=?', (status['wd'], job['id']))
            job = store.get(job['id'])
            if job['cancel_requested']:
                self.finish(job, 'cancelled', 'cancelled')
            elif status['exit_code'] != 0:
                self.finish(job, 'failed', 'launcher_exited')
            elif not job['verify'] or not report_status:
                self.finish(job, 'failed', 'verification_unavailable')
            elif job['verify'] == 'ax:manager-decision-v1':
                if job['role'] != 'manager':
                    self.finish(job, 'failed', 'verification_unavailable')
                    return
                try:
                    from core.manager.llm import parse_json
                    from core.manager.extschemas import errors
                    raw = text.strip()
                    try:
                        envelope = json.loads(raw)
                    except ValueError:
                        try:
                            envelope = json.loads(raw.splitlines()[-1])
                        except (ValueError, IndexError):
                            envelope = None
                    if isinstance(envelope, dict) and isinstance(envelope.get('result'), str):
                        raw = envelope['result']
                    decision = parse_json(raw)
                    valid = decision is not None and not errors('manager-decision', decision)
                except Exception:
                    self.finish(job, 'failed', 'verification_unavailable')
                    return
                store.record_result(job['id'], status=report_status, text=raw, human_action=human)
                job = store.get(job['id'])
                rc = 0 if valid else 1
                state = 'ok' if valid and report_status == 'ready' else 'failed'
                self.finish(job, state, 'verified' if state == 'ok' else 'verification_failed', verify_rc=rc)
            else:
                self.start_verification(job)
        elif status.get('state') in ('running', 'created', 'restarting'):
            store.set_state(job['id'], 'running', model=job['model'])

    def launch(self, job, excluded=None, capacity_wait=False):
        store = self.svc.store
        if job['cancel_requested'] and job['state'] == 'queued':
            store.set_state(job['id'], 'cancelled', reason='cancelled_before_start')
            return
        if job['cancel_requested'] and job['state'] == 'reserving':
            with store.launch_lock(job['id']):
                current = store.get(job['id'])
                if current and current['state'] == 'reserving' and current['cancel_requested']:
                    store.set_state(job['id'], 'cancelled', reason='cancelled_before_start')
                    self.release(job)
            return
        required_runner = 'chat-json' if job.get('verify') in ('ax:change-safety-review-v1', 'ax:change-injection-screen-v1') else None
        exclude_family = (job.get('request') or {}).get('_excluded_family')
        prior_model = (job.get('request') or {}).get('_excluded_model')
        excluded = set(excluded or ()) | ({prior_model} if prior_model else set())
        if (job.get('request') or {}).get('prior_job_id') and (not exclude_family or not prior_model):
            store.set_state(job['id'], 'rejected', reason='prior_job_execution_unavailable')
            return
        prompt = job['prompt']
        if job.get('attachments'):
            prompt = job_block(prompt=prompt, job_id=job['id'], attachments=job['attachments'])
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > 131072:
            store.set_state(job['id'], 'rejected', reason='bad_prompt')
            return
        models = self.svc.resources.get('/v1/resources', params={'kind': 'model'})['resources']
        if job['state'] == 'reserving':
            model = next((m for m in models if m['id'] == job['model']), None)
            if model is None or model.get('status') not in ('active', 'degraded') or job.get('reason') in ('no_eligible_candidates', 'model_unavailable') or model['id'] in excluded or not supports_runner(model, required_runner) or (exclude_family and (not isinstance(model.get('family'), str) or not model['family'].strip() or model['family'] == exclude_family)):
                with store.launch_lock(job['id']):
                    current = store.get(job['id'])
                    if not current or current['state'] != 'reserving' or current['cancel_requested']:
                        return
                    skipped = set(excluded)
                    while True:
                        pick = select(models, role=job['role'], min_level=LEVELS.get(job['role'], 'L1'),
                                      sensitivity=job['sensitivity'], needs_vision=job['needs_vision'],
                                      knobs=self.svc.selection_knobs(), hard_use=job['role'] in ('impl_hard', 'review_final'),
                                      exclude_ids=skipped, exclude_family=exclude_family, required_runner=required_runner)
                        if not pick['selected']:
                            self.svc.resources.post('/v1/slots/release', {'reservation_id': job['id']})
                            with store._lock:
                                store._conn.execute("UPDATE jobs SET reason=? WHERE id=? AND state='reserving'", (pick['reason'], job['id']))
                            return
                        candidate = pick['selected']
                        try:
                            self.svc.resources.post('/v1/slots/reassign', {'reservation_id': job['id'], 'previous_model': job['model'], 'model': candidate})
                        except ApiError as exc:
                            if exc.status != 409 or exc.code == 'reservation_conflict':
                                raise
                            skipped.add(candidate)
                            continue
                        with store._lock:
                            store._conn.execute("UPDATE jobs SET model=?, reason='' WHERE id=? AND state='reserving'", (candidate, job['id']))
                        model = next(m for m in models if m['id'] == candidate)
                        break
        else:
            pick = select(models, role=job['role'], min_level=LEVELS.get(job['role'], 'L1'),
                          sensitivity=job['sensitivity'], needs_vision=job['needs_vision'],
                          knobs=self.svc.selection_knobs(), hard_use=job['role'] in ('impl_hard', 'review_final'),
                          exclude_ids=excluded, exclude_family=exclude_family, required_runner=required_runner)
            self.svc.emit('worker.selection', job, {'job_id': job['id'], **pick})
            if not pick['selected']:
                with store._lock:
                    store._conn.execute("UPDATE jobs SET reason=? WHERE id=? AND state='queued'", (pick['reason'], job['id']))
                return
            model = next(m for m in models if m['id'] == pick['selected'])
            if capacity_wait and model.get('tier') == 'last_resort':
                with store._lock:
                    store._conn.execute("UPDATE jobs SET reason='capacity_exhausted' WHERE id=? AND state='queued'", (job['id'],))
                return
            # Intent precedes reservation and launch, so a restart never repeats a launch.
            with store._lock:
                changed = store._conn.execute("UPDATE jobs SET state='reserving',model=?,started_at=? WHERE id=? AND state='queued' AND cancel_requested=0",
                                              (model['id'], timeutil.now_ts(), job['id'])).rowcount
            if not changed:
                return
        if job.get('project'):
            try:
                timeline = getattr(self.svc, 'lesson_timeline', self.timeline)
                lessons = LessonService(None, lambda kind: timeline_events(timeline, kind))
                parts = lessons.for_worker(target=job['project'], parts=PromptParts(job=prompt))
                prompt = build(parts, arrangement=arrangement_for(model.get('prefix_cache')))
            except Exception as exc:
                with store._lock:
                    store._conn.execute("UPDATE jobs SET reason='lessons_unavailable' WHERE id=? AND state IN ('queued','reserving')", (job['id'],))
                log.warning('lessons unavailable for job %s: %s', job['id'], type(exc).__name__)
                return
        try:
            self.svc.resources.post('/v1/slots/reserve', {'reservation_id': job['id'], 'model': model['id']})
        except ApiError as exc:
            reason = exc.body.get('reason', exc.code) if isinstance(exc.body, dict) else exc.code
            if exc.status != 409 or reason not in ('capacity_exhausted', 'limits_missing', 'limits_expired', 'model_unavailable', 'model_excluded'):
                raise
            # A definite rejection created no reservation. Only then try a different model.
            with store.launch_lock(job['id']):
                current = store.get(job['id'])
                if not current or current['state'] != 'reserving':
                    return
                if current['cancel_requested']:
                    store.set_state(job['id'], 'cancelled', reason='cancelled_before_start')
                    return
                with store._lock:
                    store._conn.execute("UPDATE jobs SET state='queued', reason=? WHERE id=? AND state='reserving' AND cancel_requested=0",
                                        (reason, job['id']))
            remaining = set(excluded or ()) | {model['id']}
            current = store.get(job['id'])
            if current and current['state'] == 'queued':
                self.launch(current, excluded=remaining,
                            capacity_wait=capacity_wait or reason == 'capacity_exhausted')
            return
        with store.launch_lock(job['id']):
            current = store.get(job['id'])
            if current and current['state'] == 'cancelled' and current['cancel_requested']:
                self.release(current)
                return
            latest = self.svc.resources.get('/v1/resources', params={'kind': 'model'})['resources']
            if not any(m['id'] == model['id'] and m.get('status') in ('active', 'degraded') and supports_runner(m, required_runner) and (not exclude_family or (m.get('family') and m.get('family') == model.get('family') and m.get('family') != exclude_family)) for m in latest):
                with store._lock:
                    store._conn.execute("UPDATE jobs SET reason='model_unavailable' WHERE id=? AND state='reserving'", (job['id'],))
                return
            read_input = (job.get('request') or {}).get('read_input')
            if read_input:
                snapshot_request = {'project': job['project'], **read_input,
                                    'attachments': job.get('attachments') or []}
                try:
                    snapshot = self.launcher.job_snapshot(job['id'], snapshot_request)
                    if snapshot != {'snapshot_id': job['id'], **snapshot_request}:
                        raise LauncherError(70, 'snapshot_mismatch')
                except LauncherError as exc:
                    self.finish(store.get(job['id']), 'failed', 'snapshot_unavailable')
                    return
            with store._lock:
                changed = store._conn.execute("UPDATE jobs SET state='launching' WHERE id=? AND state='reserving' AND cancel_requested=0", (job['id'],)).rowcount
            if not changed:
                current = store.get(job['id'])
                if current and current['state'] == 'reserving' and current['cancel_requested']:
                    store.set_state(job['id'], 'cancelled', reason='cancelled_before_start')
                    self.release(job)
                return
            if not store.attempts(job['id']):
                latest_model = next(m for m in latest if m['id'] == model['id'])
                generation = (latest_model.get('stats') or {}).get('level_generation', 0)
                store.start_attempt(job['id'], model['id'], level_generation=generation, family=latest_model.get('family'), runner=required_runner or model.get('runner') or 'agent-cli')
                self.svc.emit('worker.attempt', job, {'job_id': job['id'], 'state': 'running', 'model': model['id']})
            req = {'job_id': job['id'], 'runner': required_runner or model.get('runner') or 'agent-cli', 'model': model['id'],
                   'prompt': prompt, 'allow_hosts': [], 'budget_min': job['budget_min'] or 30}
            if req['runner'] != 'chat-json':
                req['session_id'] = job['session_id']
            if required_runner:
                req['safety_profile'] = 'tool-free-chat-v1'
            if read_input:
                req['snapshot_id'] = job['id']
            for key in ('workdir_id', 'resume_from', 'tools'):
                if job.get(key):
                    req[key] = job[key]
            try:
                self.launcher.job_start(req)
            except LauncherError as exc:
                if exc.exit_code in (2, 3, 4, 5) and exc.reason not in ('empty_response', 'bad_response'):
                    self.finish(store.get(job['id']), 'failed', 'launcher_rejected')
                raise
            attempt = store.attempts(job['id'])[-1]
            store.confirm_launch(job['id'], attempt['id'])
            store.set_state(job['id'], 'running', model=model['id'])
            self.svc.emit('job.state', job, {'job_id': job['id'], 'state': 'running', 'model': model['id']})

    def tick(self):
        if not self._tick_lock.acquire(blocking=False):
            return
        fd = None
        try:
            if self.svc.store.path != ':memory:':
                fd = os.open(self.svc.store.path, os.O_RDONLY)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
            self._tick()
        finally:
            if fd is not None:
                os.close(fd)
            self._tick_lock.release()

    def allowed_during_drain(self, job, started_at, last_rowid):
        if job["role"] in ("manager", "backup_manager"):
            return True
        if not job.get("item_id") or not started_at or last_rowid is None:
            return False
        with self.svc.store._lock:
            return self.svc.store._conn.execute(
                "SELECT 1 FROM jobs WHERE item_id=? AND rowid<=? LIMIT 1",
                (job["item_id"], last_rowid)).fetchone() is not None

    def _tick(self):
        self._outcome_unavailable = False
        try:
            self.svc.resources.post('/v1/slots/reconcile-reviews', {})
        except Exception as exc:
            log.warning('review reconciliation unavailable: %s', type(exc).__name__)
        store = self.svc.store
        with self.svc._settings_lock:
            mode = self.svc._settings['mode']
            drain_started_at = self.svc.drain_started_at
            drain_last_rowid = self.svc.drain_last_rowid
        for job in store.list(limit=100000):
            if job['state'] == 'reserving':
                if mode != 'run' and not job['cancel_requested'] and not (mode == 'draining' and self.allowed_during_drain(job, drain_started_at, drain_last_rowid)):
                    continue
                try:
                    self.launch(job)
                except Exception as exc:
                    log.warning('resume reservation %s: %s', job['id'], type(exc).__name__)
                continue
            if job['state'] == 'queued':
                continue
            try:
                self.reconcile(job)
            except Exception as exc:
                log.warning('reconcile %s: %s', job['id'], type(exc).__name__)
        for job in reversed(store.list(state='queued', limit=100000)):
            if mode != 'run' and not job['cancel_requested'] and not (mode == 'draining' and self.allowed_during_drain(job, drain_started_at, drain_last_rowid)):
                continue
            try:
                self.launch(job)
            except Exception as exc:
                log.warning('dispatch %s: %s', job['id'], type(exc).__name__)
