"""Use the B16b delivery journal; add receipt-only reconciliation."""
from __future__ import annotations

from core.intake.dest.b16b.delivery import DeliveryJournal


class XJournal(DeliveryJournal):
    def confirm(self, key: tuple[str, str, str], *, external_id: str | None = None,
                definitely_not_sent: bool = False) -> dict | None:
        with self.lock:
            record = self.get(*key)
            if record is None or record["state"] != "unknown":
                return record
            if external_id:
                return self._finish(key, "sent", external_id=external_id, reason="receipt_confirmed")
            if definitely_not_sent:
                return self._finish(key, "failed", reason="confirmed_not_sent")
            return record
