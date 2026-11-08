"""T-37: publication reviews fail closed and keep sensitive data out of records."""
import json

import pytest

from plugins.subloop.release.gate.publication import (
    PublicationGate, PublicationTarget, SelectedReviewer,
)


class Scanner:
    def __init__(self, result=()):
        self.result = result
        self.calls = []

    def scan(self, text):
        self.calls.append(text)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Selector:
    def __init__(self, response=None):
        self.requests = []
        self.response = {'outcome': 'ok'} if response is None else response

    def select(self, request):
        self.requests.append(request)
        def review(payload):
            if isinstance(self.response, Exception):
                raise self.response
            return self.response
        return SelectedReviewer(request.stage, review)


class Records:
    def __init__(self):
        self.items = []

    def append(self, record):
        self.items.append(record)
        return 'evidence'


def make_gate(*, scanner=None, selector=None, pii=True, internal=None, gps=None):
    scanner, selector, records = scanner or Scanner(), selector or Selector(), Records()
    gate = PublicationGate(redactor=scanner, pii_list_available=pii,
        internal_information_check=internal or (lambda target: True),
        image_gps_check=gps or (lambda target: True), selector=selector,
        records=records, chunk_chars=20, overlap_chars=5)
    return gate, scanner, selector, records


def target(*, text='public material', history='old history', attempt=1, ref='a' * 40):
    return PublicationTarget.from_files(target_ref=ref, version='1.0', attempt=attempt,
                                       files={'README.md': text}, history=history)


def test_t37_complete_overlapping_review_and_redacted_record():
    gate, scanner, selector, records = make_gate()
    result = gate.run(target(text='private-token-example' * 4))
    assert result.ok
    assert 'old history' in scanner.calls[0]
    chunks = {}
    for request in selector.requests:
        chunks.setdefault(request.chunk.id, []).append(request)
        assert not request.tools_enabled and not request.mcp_enabled and not request.memory_enabled
    assert len(chunks) > 1
    assert all([r.stage for r in group] == ['stage-1', 'stage-2', 'stage-3'] for group in chunks.values())
    assert 'private-token-example' not in json.dumps(records.items)
    assert 'old history' not in json.dumps(records.items)


@pytest.mark.parametrize('scanner,pii,internal,gps,reason', [
    (Scanner(['hit']), True, None, None, 'secret_or_pii_detected'),
    (Scanner(RuntimeError('secret')), True, None, None, 'machine_check_unavailable'),
    (Scanner(), False, None, None, 'pii_list_missing'),
    (Scanner(), True, lambda t: False, None, 'internal_information_detected'),
    (Scanner(), True, None, lambda t: False, 'image_gps_detected'),
])
def test_t37_mechanical_failure_never_calls_review(scanner, pii, internal, gps, reason):
    gate, _, selector, records = make_gate(scanner=scanner, pii=pii, internal=internal, gps=gps)
    result = gate.run(target())
    assert not result.ok and result.reason == reason
    assert not selector.requests
    assert 'secret' not in json.dumps(records.items).replace('secret_pii', '').replace('secret_or_pii_detected', '')


@pytest.mark.parametrize('response,reason', [({'outcome': 'ng'}, 'review_ng'),
    ({'outcome': 'unsure'}, 'review_unsure'), ({'bad': 'raw-answer'}, 'review_unsure'),
    (TimeoutError('raw-answer'), 'review_unsure')])
def test_t37_unknown_or_negative_review_blocks(response, reason):
    selector = Selector(response)
    gate, _, _, records = make_gate(selector=selector)
    result = gate.run(target())
    assert not result.ok and result.reason == reason
    assert 'raw-answer' not in json.dumps(records.items)


def test_t37_new_target_is_rechecked_and_attempt_limit_blocks():
    gate, scanner, selector, _ = make_gate()
    assert gate.run(target(attempt=1)).ok
    previous = len(selector.requests)
    assert gate.run(target(text='corrected', ref='b' * 40, attempt=2)).ok
    assert len(scanner.calls) == 2 and len(selector.requests) > previous
    assert not gate.run(target(attempt=4)).ok
    assert len(scanner.calls) == 2
    assert not gate.run(target(ref='c' * 40), expected_target_ref='b' * 40).ok


def test_t37_detected_leak_survives_record_failure():
    gate, _, _, _ = make_gate(scanner=Scanner(['hit']))
    class FailingRecords:
        def append(self, record):
            raise OSError('unavailable')
    gate.records = FailingRecords()
    result = gate.run(target())
    assert not result.ok and result.reason == 'record_unavailable'
    assert result.leak_detected is True


@pytest.mark.parametrize('leak', [False, True])
def test_t37_release_path_uses_gate_and_never_publishes_detected_leak(service, dream_batch, ports, leak):
    from plugins.subloop.release.gate.publication import ReleaseGateAdapter

    gate, _, _, _ = make_gate(scanner=Scanner(['hit'] if leak else []))
    if leak:
        class FailingRecords:
            def append(self, record):
                raise OSError('unavailable')
        gate.records = FailingRecords()
    adapter = ReleaseGateAdapter(gate, lambda product, sha, notes: target(ref=sha))
    ports.gate = adapter.gate
    service.plan(dream_batch['batch_id'], latest_tag='v1.2.3',
                 changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}], diff_ref='diff:1')
    result = service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    if leak:
        assert result['reason'] == 'information_leak_detected'
        assert service.store.get('stopped:' + dream_batch['repo']) is True
        assert not any(call[0] == 'publish' for call in ports.calls)
    else:
        assert result['outcome'] == 'success'
        assert any(call[0] == 'publish' for call in ports.calls)
