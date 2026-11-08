"""Record a change-card answer after HQ has authenticated the requester."""
import json
from datetime import datetime, timezone

from axcommon.hq_proof import encode
from core.hq.approval.security import VerifiedAction

from plugins.subloop.change.integration.card_store import _PATH, _canonical, _time


def record_answer(store, card_id, decision, verified):
    if not isinstance(verified, VerifiedAction) or decision not in ('approve', 'deny'):
        raise ValueError('unauthorized')
    row = store.db.execute('SELECT scope, expires_at FROM change_cards WHERE id=?', (card_id,)).fetchone()
    if row is None or _time(row[1]) <= datetime.now(timezone.utc):
        raise ValueError('card unavailable')
    body = {'card_id': card_id, 'scope': json.loads(row[0]), 'principal': verified.identity.subject,
            'auth_method': 'hq_tailnet', 'second_factor': verified.second_factor,
            'answered_at': datetime.now(timezone.utc).isoformat(), 'expires_at': row[1], 'decision': decision}
    _, headers = encode('POST', _PATH, body, verified)
    with store.db:
        store.db.execute('INSERT INTO change_answers VALUES (?, ?, ?)',
                         (card_id, _canonical(body).decode(), headers['X-AX-HQ-Proof']))
