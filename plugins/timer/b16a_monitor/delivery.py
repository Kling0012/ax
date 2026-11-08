"""Monitor adapter for the existing durable HQ notification dispatcher."""
from core.hq.notify.dispatcher import Notification


class MonitorDelivery:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher

    def send(self, key, kind, target, detail):
        dispatcher = self.dispatcher
        with dispatcher.store._lock:
            row = dispatcher.store.by_dedupe(key)
            if row is None:
                result = dispatcher.submit(Notification(kind=kind, title='AX monitor alert',
                                                        message=f'{kind}: {target}: {detail}',
                                                        dedupe_key=key))
                row = dispatcher.store.get(result['id']) if result.get('id') else dispatcher.store.by_dedupe(key)
            elif row['state'] in ('pending', 'held'):
                dispatcher.deliver_due()
                row = dispatcher.store.get(row['id'])
            if row is None:
                return 'unknown'
            return 'sent' if row['state'] == 'sent' else 'failed'
