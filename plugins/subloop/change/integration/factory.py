"""Deployment-owned assembly for the change graph.

Dependencies without a production interface are deliberately unavailable.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path

from axcommon.client import ApiClient
from axcommon.redact import from_environment
from ..collection import CollectionService, CollectionStore, TargetRegistry
from .adapters import create_ports as assemble
from .card_store import CardStore
from .deployment_delivery import DeploymentDelivery
from .github import GitHub, _transport
from .github_delivery import GitHubDelivery
from .evidence_store import EvidenceStore
from .work import Work
from .permission import OnlinePermissionAdapter
from .ports import DependencyUnavailable


class _CollectionService(CollectionService):
    def __init__(self, store, registry, client, journal):
        super().__init__(store, registry, client)
        self.journal = journal
        self.failed_repos = set()
    def collect(self, cycle_id):
        result = super().collect(cycle_id)
        self.failed_repos = set(result['errors'])
        for repo, reason in result['errors'].items():
            self.journal.event('change.collection_error', repo, {'reason': reason})
        return result


class _Unavailable:
    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise DependencyUnavailable("dependency_unavailable:" + name)
        return unavailable


class _UnavailableSafety:
    def review(self, prepared, *, sensitivity, kind):
        return {'state': 'parked', 'reason': 'safety_reviewers_unavailable',
                'reviews': [], 'reviewer_ids': [], 'worker_input': None}


class _Control:
    def __init__(self, client):
        self.client = client
    def check_permission(self, body):
        return self.client.post('/v1/permissions/check', body)


class _Ledger:
    def __init__(self, client):
        self.client = client
    def create_todo(self, todo, *, actor):
        return self.client.create_todo(todo)


class _Cards:
    # A fresh connection per call permits the graph's worker threads to use SQLite.
    def __init__(self, path, ledger):
        self.path, self.ledger = path, _Ledger(ledger)
    def _call(self, name, *args, **kwargs):
        cards = CardStore(self.path, self.ledger)
        try:
            return getattr(cards, name)(*args, **kwargs)
        finally:
            cards.db.close()
    def wait(self, *args, **kwargs):
        return self._call('wait', *args, **kwargs)
    def verified_answer(self, *args, **kwargs):
        # Proof verification belongs to HQ; change must never receive its signing key.
        return None


class _NotifyBackend:
    def __init__(self, api):
        self.api = api
    def send(self, delivery_id, destination, body):
        if not self.api._token or destination != 'user':
            raise DependencyUnavailable('notification_unavailable')
        return self.api.post('/v1/notify/deliveries', {'delivery_id': delivery_id,
                             'destination': destination, 'body': body, 'mode': 'run'})
    def lookup(self, delivery_id, destination):
        if not self.api._token or destination != 'user':
            return {'status': 'unknown'}
        receipt = self.api.post('/v1/notify/deliveries/lookup',
                                {'delivery_id': delivery_id, 'destination': destination})
        return receipt


class _ReleaseBackend:
    def __init__(self, api, github, registry, journal):
        self.api, self.github, self.registry, self.journal = api, github, registry, journal
    def _payload(self, body):
        issue = body['issue']
        target = self.registry.targets[issue['issue_repo'].lower()]
        if target.implementation_repo != body['repo'].lower():
            raise ValueError('release target mismatch')
        item = {**issue, 'implementation_repo': target.implementation_repo, 'pr': body['pr']}
        view = self.github.view(item, asdict(target))
        if (view['merged'] is not True or view['head_sha'] != body['head_sha'] or
            view['merge_commit_sha'] != body['merge_commit_sha'] or not view['merged_at']):
            raise ValueError('merge evidence mismatch')
        payload = {'repo': target.implementation_repo, 'pr': body['pr'],
                   'merge_sha': view['merge_commit_sha'], 'merged_at': view['merged_at'],
                   'issues': [{'number': issue['number'], 'issue_repo': issue['issue_repo']}],
                   'changed_paths': view['changed_paths']}
        return payload
    def send(self, delivery_id, destination, body):
        if not self.api._token or destination != 'release':
            raise ValueError('invalid destination')
        payload = self._payload(body)
        return self.api.post('/release/deliveries', {'delivery_id': delivery_id, 'destination': destination, 'payload': payload})
    def lookup(self, delivery_id, destination):
        if not self.api._token:
            return {'status': 'unknown'}
        receipt = self.api.post('/release/deliveries/lookup', {'delivery_id': delivery_id, 'destination': destination})
        operation = self.journal.operation(delivery_id)
        if not operation or not isinstance(receipt, dict):
            return {'status': 'unknown'}
        body = operation['body']['payload']
        verified = self._payload(body)
        if (receipt.get('repo', '').lower() != verified['repo'] or receipt.get('pr') != verified['pr'] or
                receipt.get('merge_sha') != verified['merge_sha'] or receipt.get('merged_at') != verified['merged_at']):
            return {'status': 'unknown'}
        return receipt


def create_ports(*, services, journal):
    target_file = Path(os.environ.get('AX_CHANGE_TARGETS_FILE') or Path(services.plugin_dir) / 'targets.yaml')
    registry = TargetRegistry.load(target_file)
    database = journal.db.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise DependencyUnavailable('persistent_journal_required')
    data_dir = Path(database).parent
    store = CollectionStore(data_dir / 'change-collection.db')
    raw = services.github
    github = (GitHub(registry, raw._token, base_url=raw.base_url)
              if raw is not None and getattr(raw, '_token', None) else _Unavailable())
    evidence = EvidenceStore(journal, registry, store)
    if isinstance(github, GitHub):
        github.evidence = evidence.read
        github.evidence_store = evidence
    service = _CollectionService(store, registry, github, journal)

    def build_item(row, key, target):
        if key.issue_repo in service.failed_repos:
            raise DependencyUnavailable('collection_incomplete')
        payload = json.loads(row['payload'])
        # Authenticated repo-relative response, never a URL from item content.
        data = github.api.get(f'/repos/{key.issue_repo}/issues/{key.number}')
        if not isinstance(data, dict) or data.get('number') != key.number:
            raise DependencyUnavailable('item_identity_unconfirmed')
        author = (data.get('user') or {}).get('login')
        if not isinstance(author, str) or not author:
            raise DependencyUnavailable('author_unavailable')
        pr = payload.get('implementation_pr')
        number = pr.get('number') if isinstance(pr, dict) and pr.get('repo', '').lower() == target.implementation_repo else None
        return {**key.as_dict(), 'implementation_repo': target.implementation_repo,
                'pr': number, 'project': target.project or 'multi', 'author': author,
                'route': 'pr' if key.kind == 'pr' else 'change'}

    # The trusted stores for revision-bound requirements and publication do not
    # yet expose a production reader; absence never becomes merge evidence.
    redactor = from_environment()
    def outbound(value):
        try:
            return not redactor.redact_obj(value)[1]
        except Exception:
            return False
    token = getattr(services.ledger, '_token', None)
    from axcommon.change_approval import ChangeApprovalClient
    approval = ChangeApprovalClient(token) if token else None
    permissions = OnlinePermissionAdapter(control=_Control(services.control), client=approval, journal=journal)
    release_api = ApiClient(os.environ.get('AX_CHANGE_RELEASE_URL', 'http://sl-release:8080'), token, transport=_transport)
    release = DeploymentDelivery(_ReleaseBackend(release_api, github, registry, journal))
    notify_api = ApiClient(os.environ.get('AX_CHANGE_HQ_URL', 'http://hq:8083'), token, transport=_transport)
    notifications = DeploymentDelivery(_NotifyBackend(notify_api))
    unavailable = _Unavailable()
    ports = assemble(collection_service=service, journal=journal, item_builder=build_item,
                    safety_gate=_UnavailableSafety(), verification_runner=unavailable,
                    verification_request=lambda *args: (_ for _ in ()).throw(DependencyUnavailable('verification_request_unavailable')),
                    work=Work(services.pool, journal, registry), github=github, permissions=permissions,
                    notifications=notifications, publications=GitHubDelivery(github, journal),
                    release=release, outbound_check=outbound, scrub=lambda value: redactor.redact_obj(value)[0])
    from ..safety_pool import SafetyPool
    from .verification_runner import HostedVerification
    ports.safety = SafetyPool(services.pool, registry, data_dir / 'change-safety.db')
    ports.verification = HostedVerification(services.pool, journal, registry, github,
                                             scrub=lambda value: redactor.redact_obj(value)[0])
    return ports
