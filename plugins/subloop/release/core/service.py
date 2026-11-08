"""Publication orchestration. Ports are trusted adapters, never text from a PR."""
from __future__ import annotations

from copy import deepcopy
from typing import Protocol

from .registry import Held, dry_run, make_notes, next_version


class Ports(Protocol):
    def snapshot(self, product: dict) -> dict: ...
    def verify(self, product: dict, sha: str) -> list[dict]: ...
    def gate(self, product: dict, sha: str, notes: dict) -> dict: ...
    def credentials(self, product: dict) -> bool: ...
    def permission(self, scope: dict) -> dict: ...
    def promote(self, product: dict, sha: str, key: str) -> dict: ...
    def publish(self, product: dict, batch: dict, plan: dict, key: str) -> dict: ...
    def lookup(self, key: str) -> dict | None: ...
    def backmerge(self, product: dict, sha: str, key: str) -> dict: ...


def valid_evidence(evidence, sha, required):
    return all(any(e.get('kind') == kind and e.get('sha') == sha and e.get('ok') is True and e.get('ref')
                   for e in evidence) for kind in required)


def valid_gate(result, sha):
    # Adapter validates the complete coverage and independence contract (S-20.7).
    # This boundary also binds that decision to the exact publication material.
    return (result.get('ok') is True and result.get('target_sha') == sha
            and result.get('material_bound') is True and result.get('evidence_ref'))


class ReleaseService:
    def __init__(self, registry, store, ports: Ports, *, retry_limit=3, publication_gate=None):
        if publication_gate is not None:
            from ..gate.integration import GatePorts
            ports = GatePorts(ports, publication_gate)
        self.registry, self.store, self.ports = registry, store, ports
        self.retry_limit = retry_limit

    def ingest(self, **event):
        return self.store.ingest(self.registry.for_repo(event['repo']), **event)

    def deliver(self, delivery_id, destination, payload):
        return self.store.deliver(self.registry.for_repo(payload['repo']), delivery_id, destination, payload)

    def lookup_delivery(self, delivery_id, destination):
        return self.store.lookup_delivery(delivery_id, destination)

    def plan(self, batch_id, *, latest_tag, changes, diff_ref, fragment_refs=()):
        b = self.store.batch(batch_id)
        p = self.registry.get(b['product_id'])
        version = next_version(p, latest_tag, changes)
        notes = make_notes(p, version, changes, prs=b['prs'], diff_ref=diff_ref, fragment_refs=list(fragment_refs))
        plan = {'target_sha': b['target_sha'], 'version': version, 'notes': notes,
                'edition': 'normal', 'workshop_fields': ['content', 'changenote']}
        key = 'plan:' + batch_id
        previous = self.store.get(key)
        # A version and its material cannot change after an external operation began.
        if previous and previous != plan and self.store.get('publication_started:' + batch_id):
            raise Held('publication_plan_locked')
        self.store.put(key, plan)
        return deepcopy(plan)

    def _record(self, batch, outcome, reason, plan=None, *, dry=False, refs=()):
        return self.store.result(batch['batch_id'], outcome=outcome, reason=reason,
                                 version=(plan or {}).get('version'), evidence_refs=refs,
                                 dry=dry, retry_limit=self.retry_limit)

    def _scope(self, product, operation, destination=None):
        return {'project': product['project'], 'repo': product['repo'],
                'destination': destination or product['destination'], 'operation': operation}

    def _operate(self, key, batch, scope, callback):
        previous = self.store.operation(key)
        if previous:
            if previous['state'] == 'completed':
                return previous['result']
            # Reconcile, including lost receipts. A negative or ambiguous lookup never resends.
            result = self.ports.lookup(key)
            if not isinstance(result, dict) or result.get('found') is not True or not isinstance(result.get('result'), dict):
                self.store.finish_operation(key, 'lost', {'reason': 'external_result_unknown'})
                raise Held('external_result_unknown')
            result = result['result']
            self.store.finish_operation(key, 'completed', result)
            return result
        permission = self.ports.permission(scope)
        if not isinstance(permission, dict) or permission.get('allowed') is not True:
            raise Held('permission_denied')
        if not self.store.launching(key, batch['batch_id'], scope):
            # A concurrent request won the durable reservation; do not submit again.
            raise Held('operation_already_launching')
        try:
            result = callback()
            if not isinstance(result, dict):
                raise ValueError('invalid_operation_receipt')
        except Exception:
            self.store.finish_operation(key, 'lost', {'reason': 'external_result_unknown'})
            raise Held('external_result_unknown') from None
        self.store.finish_operation(key, 'completed', result)
        return result

    def execute(self, batch_id, *, explicit=None, environment=None, global_config=None):
        batch = self.store.batch(batch_id)
        p = self.registry.get(batch['product_id'])
        plan = self.store.get('plan:' + batch_id)
        if not plan:
            return self._record(batch, 'held', 'publication_plan_missing')
        dry = dry_run(explicit or {}, environment or {}, p, global_config or {})
        if dry:
            return self._record(batch, 'skipped', 'dry_run', plan, dry=True)
        if batch['state'] == 'success':
            return batch['last_result']
        if p['entry'].get('kind') == 'unresolved':
            return self._record(batch, 'held', 'publication_contract_undefined', plan)
        if not p.get('enabled', True):
            return self._record(batch, 'disabled', 'automatic_release_disabled', plan)
        if not self.ports.credentials(p):
            return self._record(batch, 'disabled', 'credentials_unverified', plan)
        if not self.store.acquire(batch_id, wait_min=p.get('lock_wait_min', 40)):
            return self._record(batch, 'held', 'release_lock_wait', plan)
        try:
            return self._execute_locked(batch, p, plan)
        except Held as error:
            # Held carries internal reason IDs only, never adapter exception strings.
            reason = str(error)
            if reason not in {'permission_denied', 'external_result_unknown', 'operation_already_launching'}:
                reason = 'publication_precondition_failed'
            return self._record(batch, 'held', reason, plan)
        except NotImplementedError:
            return self._record(batch, 'held', 'publication_contract_undefined', plan)
        except Exception:
            return self._record(batch, 'held', 'publication_dependency_unavailable', plan)
        finally:
            # Retain ownership across uncertain writes, including process restart.
            unknown = self.store.db.execute("SELECT 1 FROM ops WHERE batch=? AND state IN ('launching','submitted','lost')", (batch_id,)).fetchone()
            if not unknown:
                self.store.unlock(batch_id)

    def _execute_locked(self, batch, product, plan):
        sha = batch['target_sha']
        bid = batch['batch_id']
        published = self.store.get('published:' + product['repo'] + ':' + plan['version'])
        if published:
            if published['batch_id'] != bid:
                return self._record(batch, 'held', 'version_already_published', plan)
            return self._finish(batch, product, plan, published['receipt'])
        # A previously submitted publication is reconciled before reading a moved head.
        pubkey = f"release:{product['id']}:{plan['version']}"
        if self.store.operation(pubkey):
            receipt = self._operate(pubkey, batch, self._scope(product, 'release'), lambda: None)
            return self._published(batch, product, plan, receipt)
        snapshot = self.ports.snapshot(product)
        checks = product['checks']
        if checks.get('clean_main') and (snapshot.get('clean') is not True or snapshot.get('branch') != 'main'):
            return self._record(batch, 'held', 'worktree_not_clean_main', plan)
        if checks.get('dev_promotion'):
            if snapshot.get('dev_sha') != sha:
                self.store.put('revalidate:' + bid, {'target_sha': snapshot.get('dev_sha')})
                return self._record(batch, 'held', 'dev_head_changed_revalidate', plan)
            evidence = self.ports.verify(product, sha)
            if not valid_evidence(evidence, sha, checks['evidence']):
                return self._record(batch, 'held', 'sha_evidence_missing', plan)
            latest = self.ports.snapshot(product)
            if latest.get('dev_sha') != sha:
                self.store.put('revalidate:' + bid, {'target_sha': latest.get('dev_sha')})
                return self._record(batch, 'held', 'dev_head_changed_revalidate', plan)
            if latest.get('fast_forward') is not True:
                return self._record(batch, 'held', 'non_fast_forward_revalidate', plan)
            # Never silently promote an untrusted external PR.
            if latest.get('trusted') is not True:
                return self._record(batch, 'held', 'untrusted_pr', plan)
            if latest.get('main_sha') != sha:
                key = f'promote:{product["id"]}:{sha}'
                result = self._operate(key, batch, self._scope(product, 'merge', product['repo']),
                                       lambda: self.ports.promote(product, sha, key))
                if result.get('ok') is not True or result.get('sha') != sha:
                    return self._record(batch, 'held', 'promotion_failed', plan)
        evidence = self.ports.verify(product, sha)
        if not valid_evidence(evidence, sha, checks.get('evidence', [])):
            return self._record(batch, 'held', 'sha_evidence_missing', plan)
        gate = self.ports.gate(product, sha, plan['notes'])
        if gate.get('leak_detected'):
            self.store.put('stopped:' + product['repo'], True)
            return self._record(batch, 'failed', 'information_leak_detected', plan)
        if self.store.get('stopped:' + product['repo']):
            return self._record(batch, 'held', 'publication_stopped', plan)
        if not valid_gate(gate, sha):
            return self._record(batch, 'held', 'publication_gate_failed', plan)
        latest = self.ports.snapshot(product)
        if (checks.get('clean_main') and (latest.get('clean') is not True or latest.get('branch') != 'main'
                or latest.get('main_sha') != sha or latest.get('dev_sha') != sha)):
            return self._record(batch, 'held', 'target_sha_changed_revalidate', plan)
        if checks.get('distribution'):
            dist = latest.get('distribution', {})
            if (dist.get('version') != plan['version'] or dist.get('development_flags') is not False
                    or type(dist.get('description_bytes')) is not int
                    or dist['description_bytes'] > checks['description_max_bytes']):
                return self._record(batch, 'held', 'distribution_invalid', plan)
        # Every declared destination gets an independent permission check.
        for operation in product['operations']:
            scope = self._scope(product, operation['operation'], operation['destination'])
            if self.ports.permission(scope).get('allowed') is not True:
                return self._record(batch, 'held', 'permission_denied', plan)
        self.store.put('publication_started:' + bid, True)
        receipt = self._operate(pubkey, batch, self._scope(product, 'release'),
                                lambda: self.ports.publish(product, batch, plan, pubkey))
        return self._published(batch, product, plan, receipt)

    def _published(self, batch, product, plan, receipt):
        if receipt.get('outcome') == 'parked':
            return self._record(batch, 'held', 'environment_unavailable', plan)
        if receipt.get('outcome') == 'blocked':
            return self._record(batch, 'held', 'environment_blocked', plan)
        if receipt.get('outcome') != 'ok':
            # A post-send failure might still have published material. Keep its receipt
            # and never run the same version a second time.
            self.store.put('published:' + product['repo'] + ':' + plan['version'],
                           {'batch_id': batch['batch_id'], 'receipt': receipt})
            return self._record(batch, 'failed', 'publication_failed_no_resend', plan)
        self.store.put('published:' + product['repo'] + ':' + plan['version'],
                       {'batch_id': batch['batch_id'], 'receipt': receipt})
        return self._finish(batch, product, plan, receipt)

    def _finish(self, batch, product, plan, receipt):
        if receipt.get('outcome') != 'ok':
            return self._record(batch, 'failed', 'publication_failed_no_resend', plan)
        # Adapter must return evidence, not only a successful process exit code.
        checks = product['checks']
        if checks.get('prerelease_zip'):
            zipfile, sha_check = receipt.get('zip', {}), receipt.get('sha_check', {})
            if (receipt.get('target_sha') != batch['target_sha'] or sha_check.get('stage') != 'after'
                    or sha_check.get('ok') is not True or sha_check.get('expected') != batch['target_sha']
                    or receipt.get('prerelease') is not True or not receipt.get('tag_commit')
                    or zipfile.get('name') != checks['zip_pattern'].format(version=plan['version'])
                    or type(zipfile.get('size')) is not int or zipfile['size'] <= 0):
                return self._record(batch, 'failed', 'publication_evidence_invalid_no_resend', plan)
        if checks.get('assets'):
            from ..publish.hibi import assets_ready
            if receipt.get('version') != plan['version'] or not assets_ready(receipt.get('assets', [])):
                return self._record(batch, 'failed', 'release_assets_missing_no_resend', plan)
        # Commit success before any post-publication operation or notification.
        result = self._record(batch, 'success', 'published', plan, refs=receipt.get('evidence_refs', []))
        if 'main_to_dev' in product['post_publish']:
            key = f'backmerge:{product["id"]}:{plan["version"]}'
            try:
                merged = self._operate(key, batch, self._scope(product, 'merge', product['repo']),
                                       lambda: self.ports.backmerge(product, receipt.get('tag_commit'), key))
                if merged.get('ok') is not True:
                    self.store.put('post_publish:' + batch['batch_id'], {'state': 'held', 'reason': 'main_to_dev_conflict'})
                    with self.store.tx():
                        self.store.event('release.card', batch['batch_id'], {'reason': 'main_to_dev_conflict'})
            except Exception:
                self.store.put('post_publish:' + batch['batch_id'], {'state': 'held', 'reason': 'main_to_dev_unresolved'})
        return result
