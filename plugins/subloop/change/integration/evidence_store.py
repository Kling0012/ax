"""Revision-scoped trusted evidence; missing or stale evidence never grants a gate."""
from __future__ import annotations

from copy import deepcopy
import json

from ..collection.models import ItemKey
from ..safety.requirements import RequirementBook
from .gate import full_sha, independent_safe, valid_verification
from .flow import item_key


class EvidenceStore:
    def __init__(self, journal, registry, collection_store):
        self.journal, self.registry, self.collection_store = journal, registry, collection_store

    def _scope(self, item, facts):
        key = ItemKey(**{k: item[k] for k in ('provider', 'issue_repo', 'kind', 'number')})
        target = self.registry.resolve(key)
        repo, pr, head, revision = facts.get('repo'), facts.get('pr'), facts.get('head_sha'), facts.get('input_revision')
        if (target is None or not isinstance(repo, str) or repo.lower() != target.implementation_repo or
                item.get('implementation_repo', '').lower() != target.implementation_repo or
                not full_sha(head) or not isinstance(revision, str) or not revision):
            raise ValueError('evidence_scope_mismatch')
        if facts.get('snapshot_type') == 'issue':
            if (key.kind != 'issue' or pr is not None or item.get('pr') is not None or
                    not target.project or not target.base_ref or facts.get('project') != target.project or
                    item.get('project') != target.project or facts.get('base_ref') != target.base_ref or
                    facts.get('base_sha') != head or facts.get('source_issue_repo') != key.issue_repo or
                    facts.get('source_issue_number') != key.number or facts.get('comments_complete') is not True or
                    facts.get('diff') != ''):
                raise ValueError('evidence_scope_mismatch')
            kind = 'issue'
        else:
            if type(pr) is not int or pr <= 0 or item.get('pr') != pr:
                raise ValueError('evidence_scope_mismatch')
            kind = 'pr'
        scope = {'item': key.as_dict(), 'repo': repo.lower(), 'pr': pr, 'head': head, 'revision': revision}
        if kind == 'issue':
            scope['type'] = 'issue'
        return json.dumps(scope, sort_keys=True, separators=(',', ':'))

    def record_safety(self, item, facts, safety):
        scope = self._scope(item, facts)
        if not independent_safe(safety, head=facts['head_sha'], revision=facts['input_revision']):
            raise ValueError('safety_unconfirmed')
        self.journal.put('review_evidence', scope, {'base_sha': facts['base_sha'], 'safety': safety})

    def record_verification(self, item, facts, verification):
        if facts.get('snapshot_type') == 'issue':
            raise ValueError('pr_evidence_required')
        scope = self._scope(item, facts)
        previous = self.journal.get('review_evidence', scope)
        target = self.registry.resolve(ItemKey(**{k: item[k] for k in ('provider', 'issue_repo', 'kind', 'number')}))
        if (not previous or previous.get('base_sha') != facts['base_sha'] or
                not independent_safe(previous.get('safety'), head=facts['head_sha'], revision=facts['input_revision']) or
                not valid_verification(verification, repo=target.implementation_repo, base=facts['base_sha'],
                                       head=facts['head_sha'], untrusted=item.get('author') not in target.trusted_authors)):
            raise ValueError('verification_unconfirmed')
        self.journal.put('verification_evidence', scope, {'base_sha': facts['base_sha'], 'verification': verification})

    def _comments_current(self, item, facts, positions):
        if not isinstance(positions, dict):
            return False
        for group in ('comments', 'review_comments', 'source_comments'):
            if group == 'source_comments':
                key = ItemKey(**{k: item[k] for k in ('provider', 'issue_repo', 'kind', 'number')})
            else:
                key = ItemKey('github', facts['repo'], 'pr', facts['pr'])
            with self.collection_store.transaction() as db:
                rows = db.execute('SELECT id,version FROM comments WHERE key=?', (key.encode(),)).fetchall()
            ack = {str(row['id']): json.loads(row['version']) for row in rows}
            for comment in facts.get(group, []):
                cid, revision = str(comment['id']), comment['updated_at']
                acknowledged = ack.get(cid)
                if (not isinstance(revision, str) or not revision or positions.get(group + ':' + cid) != revision or
                        not isinstance(acknowledged, dict) or acknowledged.get('revision', acknowledged.get('updated_at')) != revision):
                    return False
        return True

    def save_reviewed(self, item, facts, *, safety, verification, requirements, positions, publication_id):
        """Called only by trusted pipeline after processing; never use raw PR claims."""
        if facts.get('snapshot_type') == 'issue':
            raise ValueError('pr_evidence_required')
        scope = self._scope(item, facts)
        target = self.registry.resolve(ItemKey(**{k: item[k] for k in ('provider', 'issue_repo', 'kind', 'number')}))
        if not independent_safe(safety, head=facts['head_sha'], revision=facts['input_revision']):
            raise ValueError('safety_unconfirmed')
        if not valid_verification(verification, repo=target.implementation_repo, base=facts.get('base_sha'),
                                  head=facts['head_sha'], untrusted=item.get('author') not in target.trusted_authors):
            raise ValueError('verification_unconfirmed')
        book = RequirementBook(requirements)
        if not self._comments_current(item, facts, positions):
            raise ValueError('comments_unconfirmed')
        op = self.journal.operation(publication_id)
        if not op or op['state'] != 'succeeded' or op['kind'] != 'delivery':
            raise ValueError('publication_unconfirmed')
        payload, result = op['body'].get('payload', {}), op['result'] or {}
        if (not publication_id.startswith('pr:') or not publication_id.endswith(':' + facts['head_sha'] + ':' + facts['input_revision']) or
                payload.get('source_item') != item_key(item) or op['body'].get('destination') != 'github' or payload.get('repo', '').lower() != target.implementation_repo or
                payload.get('pr') != facts['pr'] or payload.get('head_sha') != facts['head_sha'] or
                result.get('status') != 'delivered'):
            raise ValueError('publication_scope_mismatch')
        self.journal.put('verified_evidence', scope, {'safety': safety, 'verification': verification,
            'requirements': book.records, 'positions': positions, 'publication_id': publication_id,
            'base_sha': facts['base_sha']})

    def read(self, item, target, facts):
        try:
            scope = self._scope(item, facts)
            row = self.journal.get('verified_evidence', scope)
            if not isinstance(row, dict) or row.get('base_sha') != facts.get('base_sha'):
                return {}
            if not independent_safe(row['safety'], head=facts['head_sha'], revision=facts['input_revision']):
                return {}
            registered = self.registry.resolve(ItemKey(**{k: item[k] for k in ('provider', 'issue_repo', 'kind', 'number')}))
            if not valid_verification(row['verification'], repo=registered.implementation_repo, base=facts['base_sha'],
                                      head=facts['head_sha'], untrusted=item.get('author') not in registered.trusted_authors):
                return {}
            if not self._comments_current(item, facts, row['positions']):
                return {}
            op = self.journal.operation(row['publication_id'])
            if not op or op['state'] != 'succeeded' or (op['result'] or {}).get('status') != 'delivered':
                return {}
            payload = op['body'].get('payload', {})
            publication_id = row['publication_id']
            if (not publication_id.startswith('pr:') or not publication_id.endswith(':' + facts['head_sha'] + ':' + facts['input_revision']) or
                    payload.get('source_item') != item_key(item) or op['body'].get('destination') != 'github' or payload.get('repo', '').lower() != registered.implementation_repo or
                    payload.get('pr') != facts['pr'] or payload.get('head_sha') != facts['head_sha']):
                return {}
            records = RequirementBook(row['requirements']).records
            return {'unprocessed_comments': [], 'requirements_revision': facts['input_revision'],
                    'required_ids': [r['id'] for r in records], 'requirements': deepcopy(records),
                    'publication_head': facts['head_sha'], 'outbound_checked': True}
        except Exception:
            return {}
