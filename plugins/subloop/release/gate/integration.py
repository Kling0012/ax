"""Wire a fixed publication target and durable review attempts into release ports."""
from __future__ import annotations

from copy import deepcopy
import json
import subprocess

from .publication import MAX_ATTEMPTS, PublicationTarget, ReleaseGateAdapter


class GitPublicationSource:
    """Read the complete tree and history from a trusted local repository resolver.

    Non-text objects fail closed until a binary-aware scanner is provided.
    """
    def __init__(self, resolve_repository):
        self.resolve_repository = resolve_repository

    def collect(self, product, sha, notes):
        if hasattr(self.resolve_repository, 'entry'):
            repository = self.resolve_repository.entry(product)
        else:
            repository = {'path': self.resolve_repository(product), 'visibility': 'PUBLIC'}
        path = repository['path']
        def git(*args):
            return subprocess.run(['git', '-C', str(path), *args], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        entries = git('ls-tree', '-rz', '--full-tree', sha).split(b'\0')
        files = {}
        for entry in filter(None, entries):
            metadata, raw_path = entry.split(b'\t', 1)
            mode, kind, oid = metadata.split(b' ', 2)
            if kind != b'blob' or mode == b'120000':
                raise ValueError('unsupported_tree_entry')
            name = raw_path.decode('utf-8', 'strict')
            value = git('cat-file', 'blob', oid.decode('ascii'))
            if value.startswith((b'\xff\xd8', b'\x89PNG\r\n\x1a\n', b'II*\x00', b'MM\x00*')):
                files[name] = value
            else:
                if b'\0' in value:
                    raise ValueError('binary_requires_scanner')
                files[name] = value.decode('utf-8', 'strict')
        if not files:
            raise ValueError('empty_tree')
        if '[release-notes]' in files:
            raise ValueError('reserved_material_path')
        files['[release-notes]'] = json.dumps(notes, ensure_ascii=False, sort_keys=True)
        history = git('log', '--all', '--format=fuller', '-p', '--no-ext-diff', '--no-textconv').decode('utf-8', 'strict')
        if hasattr(self.resolve_repository, 'entry'):
            return files, history, repository['visibility'].lower()
        return files, history


class PersistentReleaseGate:
    """Reserve an attempt durably before collecting or reviewing any material."""
    def __init__(self, gate, source, store, *, create_identity_card=None):
        self.source, self.store = source, store
        self.gate_runner, self.create_identity_card = gate, create_identity_card

    def gate(self, product, sha, notes):
        # A changed SHA does not reset the retry budget for the same version.
        key = 'publish_review_attempt:' + product['repo'] + ':' + notes['version']
        with self.store.tx():
            attempt = self.store.get(key, 0) + 1
            if attempt <= MAX_ATTEMPTS:
                self.store.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',
                                      (key, json.dumps(attempt)))
        if attempt > MAX_ATTEMPTS:
            return {'ok': False, 'target_sha': sha, 'material_bound': False,
                    'reason': 'attempt_limit', 'leak_detected': False}
        # Each invocation has its own adapter, avoiding shared mutable attempt state.
        def target(p, s, n):
            collected = self.source.collect(p, s, n)
            files, history = collected[:2]
            sensitivity = collected[2] if len(collected) == 3 else 'public'
            return PublicationTarget.from_files(target_ref=s, version=n['version'], attempt=attempt,
                                                files=files, history=history, sensitivity=sensitivity)
        try:
            material = target(deepcopy(product), sha, deepcopy(notes))
        except Exception:
            missing = PublicationTarget.from_files(target_ref=sha, version=notes['version'],
                                                   attempt=attempt, files={}, history='')
            return self.gate_runner._complete(missing, [], 'failed', 'target_binding_invalid').as_release_port()
        return ReleaseGateAdapter(self.gate_runner, lambda p, s, n: material,
                                  create_identity_card=self.create_identity_card).gate(
                                      deepcopy(product), sha, deepcopy(notes))


class GatePorts:
    """Override only the gate port while forwarding all other release operations."""
    def __init__(self, ports, gate):
        self._ports, self.gate = ports, gate.gate

    def __getattr__(self, name):
        return getattr(self._ports, name)
