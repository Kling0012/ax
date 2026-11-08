import hashlib
import json

import pytest

from plugins.subloop.release.gate.publication import JsonlReviewStore, PublicationGate, PublicationTarget
from plugins.subloop.release.gate.scanners import InternalScanner, SecretScanner, S8ReviewerSelector, image_gps_clear


@pytest.mark.parametrize('case', ['allowed', 'path', 'rule', 'missing', 'history', 'history_path'])
def test_scoped_allowlist_gate(tmp_path, monkeypatch, case):
    pii = tmp_path / 'pii'; pii.write_text('')
    hosts = tmp_path / 'hosts'; hosts.write_text('')
    terms = tmp_path / 'terms'; terms.write_text('')
    line = 'password' + '=' + 'verysecretvalue123'
    rule = 'token:assign' if case != 'rule' else 'pii:email'
    path = 'other.txt' if case in ('path', 'history_path') else 'file.txt'
    allow = tmp_path / 'allow.json'
    if case != 'missing':
        allow.write_text(json.dumps([{'rule': rule, 'path': path, 'sha256': hashlib.sha256(line.encode()).hexdigest()}]))
    monkeypatch.setenv('AX_PUBLICATION_ALLOWLIST', str(allow))
    history = 'diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n@@ -0,0 +1 @@\n+' + line if case.startswith('history') else ''
    files = {'file.txt': 'clean' if history else line}
    target = PublicationTarget.from_files(target_ref='abc', version='v1', attempt=1, files=files, history=history)
    scanner = SecretScanner(pii); scanner.binary = None
    records = tmp_path / 'record.jsonl'
    gate = PublicationGate(redactor=scanner, pii_list_available=True,
                           internal_information_check=InternalScanner(hosts, terms), image_gps_check=image_gps_clear,
                           selector=S8ReviewerSelector(lambda p: {'id': 'model', 'family': 'free'}, lambda m, p: {'outcome': 'ok'}),
                           records=JsonlReviewStore(records))
    result = gate.run(target, expected_target_ref='abc')
    assert result.ok is (case in ('allowed', 'history'))
    assert line not in records.read_text()
    findings = result.record['checks'][0]['findings']
    assert any(f.get('status') == 'allowed' for f in findings) is (case in ('allowed', 'history'))
