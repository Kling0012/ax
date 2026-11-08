"""Journal-bound GitHub deliveries. Unknown outcomes are never retried here."""
from __future__ import annotations

from urllib.parse import quote

import hashlib
import json
import re
import time


class GitHubDelivery:
    def __init__(self, github, journal):
        self.github, self.journal = github, journal

    @staticmethod
    def _marker(delivery_id, body):
        identity = hashlib.sha256(delivery_id.encode()).hexdigest()
        payload = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        return f'<!-- ax-delivery:{identity}:{payload} -->', f'<!-- ax-delivery:{identity}:'

    def _inspect(self, delivery_id, destination):
        op = self.journal.operation(delivery_id)
        if not op or destination != 'github' or op['body'].get('destination') != destination:
            return None, None
        body = op['body']['payload']
        repo = self.github._repo(body['repo'], issue='number' in body and 'pr' not in body)
        marker, prefix = self._marker(delivery_id, body)
        if 'pr' in body:
            rows = self.github._pages(f'/repos/{repo}/pulls', params={'state': 'all'})
        else:
            number = self.github._number(body['number'])
            rows = self.github._pages(f'/repos/{repo}/issues/{number}/comments')
        marked = [row for row in rows if prefix in (row.get('body') or '')]
        if not marked:
            return body, None
        if len(marked) != 1:
            return body, {'status': 'unknown'}
        row = marked[0]
        expected_body = body['body'] + '\n\n' + marker
        if row.get('body') != expected_body:
            return body, {'status': 'unknown'}
        if 'pr' in body:
            pr = row.get('number')
            if body['pr'] is not None and body['pr'] != pr:
                return body, {'status': 'unknown'}
            _, row = self.github._pull(repo, pr)
            if (row.get('body') != expected_body or row.get('title') != body.get('title') or
                    row.get('head', {}).get('sha') != body.get('head_sha') or
                    (body['pr'] is None and (row.get('head', {}).get('ref') != body.get('head') or
                                               row.get('base', {}).get('ref') != body.get('base')))):
                return body, {'status': 'unknown'}
            return body, {'status': 'delivered', 'delivered_at': time.time(), 'repo': repo, 'pr': pr}
        return body, {'status': 'delivered', 'delivered_at': time.time()}

    def reconcile(self, delivery_id, destination):
        try:
            _, result = self._inspect(delivery_id, destination)
            return result or {'status': 'unknown'}
        except Exception:
            return {'status': 'unknown'}

    def send(self, delivery_id, destination, body):
        stored, result = self._inspect(delivery_id, destination)
        if stored != body or result is not None:
            return result or {'status': 'unknown'}
        marker, _ = self._marker(delivery_id, body)
        rendered = body['body'] + '\n\n' + marker
        repo = body['repo'].lower()
        if 'pr' not in body:
            self.github.api.post(f"/repos/{repo}/issues/{self.github._number(body['number'])}/comments", {'body': rendered})
        elif body['pr'] is not None:
            _, pull = self.github._pull(repo, body['pr'])
            if pull.get('head', {}).get('sha') != body.get('head_sha'):
                return {'status': 'unknown'}
            self.github.api.request('PATCH', f"/repos/{repo}/pulls/{body['pr']}", json_body={'title': body['title'], 'body': rendered})
        else:
            head, base = body.get('head'), body.get('base')
            if not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_./-]+', v) and '..' not in v for v in (head, base)):
                return {'status': 'unknown'}
            ref = self.github.api.get(f'/repos/{repo}/git/ref/heads/{quote(head, safe="/")}')
            if not isinstance(ref, dict) or (ref.get('object') or {}).get('sha') != body.get('head_sha'):
                return {'status': 'unknown'}
            self.github.api.post(f'/repos/{repo}/pulls', {'title': body['title'], 'body': rendered, 'head': head, 'base': base})
        return self.reconcile(delivery_id, destination)
