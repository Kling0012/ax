"""Fake release dependencies. They only record calls."""
from __future__ import annotations


SHA = 'a' * 40


class FakeReleasePorts:
    def __init__(self, *, sha=SHA):
        self.sha = sha
        self.allowed = True
        self.credentials_ok = True
        self.gate_result = {'ok': True, 'target_sha': sha, 'material_bound': True, 'evidence_ref': 'gate:1'}
        self.snapshot_data = {
            'clean': True, 'branch': 'main', 'dev_sha': sha, 'main_sha': sha,
            'fast_forward': True, 'trusted': True,
            'distribution': {'version': 'v1.2.4', 'development_flags': False, 'description_bytes': 7900},
        }
        self.calls = []
        self.receipts = {}

    def snapshot(self, product):
        self.calls.append(('snapshot', product['id']))
        return dict(self.snapshot_data)

    def verify(self, product, sha):
        self.calls.append(('verify', sha))
        return [{'kind': name, 'sha': sha, 'ok': True, 'ref': f'evidence:{name}'}
                for name in product['checks'].get('evidence', [])]

    def gate(self, product, sha, notes):
        self.calls.append(('gate', sha))
        return dict(self.gate_result)

    def credentials(self, product):
        self.calls.append(('credentials', product['id']))
        return self.credentials_ok

    def permission(self, scope):
        self.calls.append(('permission', scope['operation'], scope['destination']))
        return {'allowed': self.allowed}

    def promote(self, product, sha, key):
        self.calls.append(('promote', key))
        return {'ok': True, 'sha': sha}

    def publish(self, product, batch, plan, key):
        self.calls.append(('publish', key))
        result = {'outcome': 'ok', 'target_sha': batch['target_sha'], 'tag_commit': 'b' * 40,
                  'sha_check': {'stage': 'after', 'ok': True, 'expected': batch['target_sha']},
                  'prerelease': True, 'zip': {'name': f"DreamforgeRPG-{plan['version']}.zip", 'size': 1},
                  'evidence_refs': ['release:1']}
        self.receipts[key] = result
        return result

    def lookup(self, key):
        self.calls.append(('lookup', key))
        result = self.receipts.get(key)
        return {'found': True, 'result': result} if result else None

    def backmerge(self, product, sha, key):
        self.calls.append(('backmerge', key))
        return {'ok': True, 'sha': sha}
