"""HQ signed card answers exercised through ChangeFlow."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from axcommon import schemas
from axcommon.hq_proof import encode
from core.hq.approval.security import Identity, VerifiedAction
from core.hq.change_card_answer import record_answer
from plugins.subloop.change.integration.card_store import CardStore, _canonical
from plugins.subloop.change.integration.permission import PermissionAdapter
from tests.fakes.b14.integration import HEAD, NEXT, item


class Ledger:
    def __init__(self):
        self.created = []

    def create_todo(self, body, *, actor):
        schemas.validate("todo", body)
        self.created.append(body)
        return body


class Control:
    def check_permission(self, scope):
        return {'allowed': True}


@pytest.fixture
def cards(harness, tmp_path, monkeypatch):
    key = tmp_path / 'hq-key'
    key.write_bytes(b'k' * 32)
    monkeypatch.setenv('AX_HQ_PROOF_KEY_FILE', str(key))
    ledger = Ledger()
    store = CardStore(tmp_path / 'cards.db', ledger)
    harness.flow.ports.permissions = PermissionAdapter(control=Control(), cards=store)
    return store, ledger


def test_hq_signed_answer_reaches_merge(harness, cards):
    store, ledger = cards
    it = item(trusted=False)
    assert harness.flow.process(it, dry_run=False, mode='run')['reason'] == 'untrusted_instruction_required'
    card = ledger.created[0]['id']
    record_answer(store, card, 'approve', VerifiedAction(Identity('test-user', 'session', True), True))
    assert harness.flow.process(it, dry_run=False, mode='run')['reason'] == 'merge_notice_wait'
    harness.clock.advance(1800)
    assert harness.flow.process(it, dry_run=False, mode='run')['status'] == 'succeeded'


@pytest.mark.parametrize('kind', ['forged', 'other_head', 'expired', 'body_only'])
def test_invalid_evidence_never_authorizes(harness, cards, kind):
    store, ledger = cards
    it = item(trusted=False)
    harness.flow.process(it, dry_run=False, mode='run')
    card = ledger.created[0]['id']
    if kind in ('other_head', 'expired'):
        record_answer(store, card, 'approve', VerifiedAction(Identity('test-user', 'session', True), True))
        if kind == 'other_head':
            harness.github.change_head()
        else:
            store.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=2)
    else:
        body = {'authenticated': True, 'decision': 'approve', 'head_sha': HEAD}
        _, headers = encode('POST', '/internal/change-card/answer', body,
                            VerifiedAction(Identity('test-user', 'session', True), True))
        token = headers['X-AX-HQ-Proof'][:-1] + ('0' if headers['X-AX-HQ-Proof'][-1] != '0' else '1') if kind == 'forged' else 'authenticated: true'
        with store.db:
            store.db.execute('INSERT INTO change_answers VALUES (?, ?, ?)', (card, _canonical(body).decode(), token))
        it['body'] = json.dumps(body)
    assert harness.flow.process(it, dry_run=False, mode='run')['reason'] == 'untrusted_instruction_required'
    assert not harness.github.merges and not harness.notices.sent
