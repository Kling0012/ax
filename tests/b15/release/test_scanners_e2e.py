"""Exercise production scanners through the release service."""
import json
import struct

import pytest

from plugins.subloop.release.core.service import ReleaseService
from plugins.subloop.release.gate.integration import PersistentReleaseGate
from plugins.subloop.release.gate.publication import JsonlReviewStore, PublicationGate
from plugins.subloop.release.gate.scanners import (
    InternalScanner, S8ReviewerSelector, SecretScanner, image_gps_clear,
)


@pytest.mark.parametrize('case', ['clean', 'secret', 'gps', 'internal_ip', 'unavailable', 'claude', 'missing_pii', 'scanner_error', 'fallback'])
def test_release_scanners(service, dream_batch, ports, tmp_path, case):
    pii = tmp_path / 'pii'; pii.write_text('', encoding='utf8')
    hosts = tmp_path / 'hosts'; hosts.write_text('privatehost\n', encoding='utf8')
    terms = tmp_path / 'terms'; terms.write_text('privateworkterm\n', encoding='utf8')
    records = tmp_path / 'reviews.jsonl'
    files = {'README': 'A public release.'}
    secret = 'password' + '=' + 'very' + 'secretvalue123'
    if case == 'secret':
        files['README'] = secret
    if case == 'internal_ip':
        files['README'] = 'Address 100.' + '64.1.1'
    if case == 'gps':
        # TIFF IFD0 with a GPSInfo pointer tag.
        files['photo.tif'] = b'II' + struct.pack('<HIH', 42, 8, 1) + struct.pack('<HHII', 0x8825, 4, 1, 26) + b'\0'*4

    class Source:
        def collect(self, product, sha, notes):
            return files, 'public history'

    def choose(payload):
        if case == 'unavailable':
            return None
        if case == 'claude':
            return {'id': 'claude-test', 'family': 'claude'}
        return {'id': 'review-model', 'family': 'free'}

    scanner = SecretScanner(tmp_path / 'missing' if case == 'missing_pii' else pii)
    if case == 'fallback':
        scanner.binary = None
    if case == 'scanner_error':
        scanner.binary = '/nonexistent/gitleaks'
    gate = PublicationGate(redactor=scanner, pii_list_available=True,
                           internal_information_check=InternalScanner(hosts, terms),
                           image_gps_check=image_gps_clear,
                           selector=S8ReviewerSelector(choose, lambda model, payload: {'outcome': 'ok'}),
                           records=JsonlReviewStore(records))
    wired = ReleaseService(service.registry, service.store, ports,
                           publication_gate=PersistentReleaseGate(gate, Source(), service.store))
    service.ports = wired.ports
    service.plan(dream_batch['batch_id'], latest_tag='v1.2.3',
                 changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}], diff_ref='diff:1')
    result = service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    published = any(call[0] == 'publish' for call in ports.calls)
    assert published is (case in ('clean', 'fallback'))
    record_text = records.read_text(encoding='utf8')
    record = json.loads(record_text)
    assert record['outcome'] == ('passed' if case in ('clean', 'fallback') else 'failed')
    assert secret not in record_text
    if case in ('secret', 'gps', 'internal_ip'):
        assert any(check.get('findings') for check in record['checks'])
    if case in ('unavailable', 'claude'):
        assert record['reason'] == 'review_unsure'


def test_git_image_collection_through_release(service, dream_batch, ports, tmp_path):
    import subprocess
    from plugins.subloop.release.gate.integration import GitPublicationSource
    from tests.fakes.b15.release.ports import SHA

    repo = tmp_path / 'repo'; repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()
    git('init', '-q')
    git('config', 'user.email', 'tester@example.com')
    git('config', 'user.name', 'Tester')
    (repo / 'image.tif').write_bytes(b'II' + struct.pack('<HIH', 42, 8, 1) + struct.pack('<HHII', 0x8825, 4, 1, 26) + b'\0'*4)
    git('add', 'image.tif')
    git('commit', '-qm', 'Add image')
    actual_sha = git('rev-parse', 'HEAD')
    class Source:
        def collect(self, product, sha, notes):
            assert sha == SHA
            return GitPublicationSource(lambda p: repo).collect(product, actual_sha, notes)
    pii = tmp_path / 'pii'; pii.write_text('')
    hosts = tmp_path / 'hosts'; hosts.write_text('')
    terms = tmp_path / 'terms'; terms.write_text('')
    records = tmp_path / 'records'
    gate = PublicationGate(redactor=SecretScanner(pii), pii_list_available=True,
                           internal_information_check=InternalScanner(hosts, terms), image_gps_check=image_gps_clear,
                           selector=S8ReviewerSelector(lambda p: {'id': 'model', 'family': 'free'},
                                                       lambda m, p: {'outcome': 'ok'}),
                           records=JsonlReviewStore(records))
    service.ports = ReleaseService(service.registry, service.store, ports,
                                  publication_gate=PersistentReleaseGate(gate, Source(), service.store)).ports
    service.plan(dream_batch['batch_id'], latest_tag='v1.2.3',
                 changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}], diff_ref='diff:1')
    service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    assert not any(call[0] == 'publish' for call in ports.calls)
    assert json.loads(records.read_text())['reason'] == 'image_gps_detected'


def test_git_secret_locations_block_publication(tmp_path):
    """The recorded locations match the original per-source stdin scanner."""
    import random
    import string
    import subprocess
    from collections import Counter
    from plugins.subloop.release.gate.integration import GitPublicationSource
    from plugins.subloop.release.gate.publication import PublicationTarget

    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()
    git('init', '-q')
    git('config', 'user.email', 'tester@example.com')
    git('config', 'user.name', 'Tester')
    rng = random.Random(9124)
    def token():
        return 'gh' + 'p_' + ''.join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))
    path_token, body_token, historic_token = token(), token(), token()
    file = repo / ('key-' + path_token)
    file.write_text('first line\n' + historic_token + '\n')
    git('add', '.')
    git('commit', '-qm', 'first revision')
    file.write_text('first line\n' + body_token + '\n')
    git('add', '.')
    git('commit', '-qm', 'second revision')
    sha = git('rev-parse', 'HEAD')
    files, history = GitPublicationSource(lambda product: repo).collect({}, sha, {})
    target = PublicationTarget.from_files(target_ref=sha, version='1', attempt=1,
                                          files=files, history=history)
    pii = tmp_path / 'pii'; pii.write_text('')
    records = tmp_path / 'records'
    gate = PublicationGate(redactor=SecretScanner(pii), pii_list_available=True,
                           internal_information_check=lambda target: True,
                           image_gps_check=lambda target: True,
                           selector=S8ReviewerSelector(lambda payload: None, lambda model, payload: None),
                           records=JsonlReviewStore(records))
    result = gate.run(target)
    assert not result.ok
    assert result.reason == 'secret_or_pii_detected'
    findings = result.record['checks'][0]['findings']
    actual = Counter((entry['location'], entry['type']) for entry in findings)
    # Captured from commit 46cc841 using this same repository fixture.
    expected = Counter({
        ('file:1:line:1', 'token:gitleaks'): 1,
        ('file:1:line:2', 'token:gitleaks'): 1,
        ('file:1:line:2', 'token:prefixed'): 1,
        ('history:line:9', 'token:gitleaks'): 2,
        ('history:line:11', 'token:gitleaks'): 1,
        ('history:line:12', 'token:gitleaks'): 1,
        ('history:line:15', 'token:gitleaks'): 1,
        # Deleted tokens remain visible in public history.
        ('history:line:15', 'token:prefixed'): 1,
        ('history:line:16', 'token:gitleaks'): 1,
        ('history:line:16', 'token:prefixed'): 1,
        ('history:line:26', 'token:gitleaks'): 2,
        ('history:line:30', 'token:gitleaks'): 1,
        ('history:line:33', 'token:gitleaks'): 1,
        ('history:line:33', 'token:prefixed'): 1,
    })
    assert actual == expected
    assert all(secret not in records.read_text() for secret in (path_token, body_token, historic_token))


@pytest.mark.parametrize('line, detected', [
    ('+@pytest.mark.parametrize', False),
    ('+mail fixture@example.invalid', True),
    ('-mail fixture@example.invalid', True),
])
def test_history_diff_markers_through_publication_gate(tmp_path, line, detected):
    from plugins.subloop.release.gate.publication import PublicationTarget

    pii = tmp_path / 'pii'; pii.write_text('')
    hosts = tmp_path / 'hosts'; hosts.write_text('')
    terms = tmp_path / 'terms'; terms.write_text('')
    records = tmp_path / 'records'
    history = 'diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n' + line + '\n'
    target = PublicationTarget.from_files(target_ref='abc', version='v1', attempt=1,
                                          files={'f': 'clean'}, history=history)
    gate = PublicationGate(redactor=SecretScanner(pii), pii_list_available=True,
                           internal_information_check=InternalScanner(hosts, terms),
                           image_gps_check=image_gps_clear,
                           selector=S8ReviewerSelector(lambda p: {'id': 'model', 'family': 'free'},
                                                       lambda m, p: {'outcome': 'ok'}),
                           records=JsonlReviewStore(records))
    result = gate.run(target)
    assert result.ok is not detected
