"""Durable adapters for HQ second-factor security events."""
from __future__ import annotations

import ulid

from .dispatcher import Notification


class FactorDelivery:
    def __init__(self, sink, dispatcher):
        self.sink = sink
        self.dispatcher = dispatcher

    def emit(self, item):
        if self.sink is None:
            raise RuntimeError('factor_event_unavailable')
        event_id = str(ulid.ULID.from_bytes(bytes.fromhex(item['id'])))
        self.sink.emit(item['kind'], 'approval.factor', item['data'], event_id=event_id)

    def notify(self, item):
        if self.dispatcher is None:
            raise RuntimeError('factor_notification_unavailable')
        messages = {
            'approval.factor.enrolled': 'The approval second factor was registered or changed.',
            'auth.failed': 'An approval second-factor verification failed.',
        }
        message = messages[item['kind']]
        dispatcher = self.dispatcher
        key = 'factor:' + item['id']
        with dispatcher.store._lock:
            row = dispatcher.store.by_dedupe(key)
            if row is None:
                result = dispatcher.submit(Notification(kind=item['kind'], title='AX security notice',
                                                        message=message, dedupe_key=key))
                row = dispatcher.store.get(result['id']) if result['id'] else dispatcher.store.by_dedupe(key)
            if row is None or row['state'] != 'sent':
                raise RuntimeError('factor_notification_pending')
