"""DeliveryPort backed by a deployment-provided, idempotent receipt service.

The service exposes send(id, destination, body) and lookup(id, destination).
Lookup returns a receipt bound to both identifiers, or a retry. An absence
is treated as unknown because a previous write may still be in flight. A send
response alone is never evidence of delivery.
"""
from __future__ import annotations

import math
import time


class DeploymentDelivery:
    def __init__(self, backend, *, clock=time.time):
        self.backend = backend
        self.clock = clock

    def reconcile(self, delivery_id: str, destination: str) -> dict:
        try:
            receipt = self.backend.lookup(delivery_id, destination)
        except Exception:
            return {"status": "unknown"}
        if not isinstance(receipt, dict):
            return {"status": "unknown"}
        if receipt.get("delivery_id") != delivery_id or receipt.get("destination") != destination:
            return {"status": "unknown"}
        status = receipt.get("status")
        # Lookup identifies only ID and destination, not the submitted body.
        # A lost send response cannot prove the remote receipt belongs to our body.
        if destination == "user" and receipt.get("accepted") is True:
            return {"status": "unknown"}
        if status == "delivered":
            timestamp = receipt.get("delivered_at")
            if type(timestamp) in (int, float) and math.isfinite(timestamp) and 0 <= timestamp <= self.clock():
                result = {"status": "delivered", "delivered_at": timestamp}
                if "repo" in receipt or "pr" in receipt:
                    repo, pr = receipt.get("repo"), receipt.get("pr")
                    if (not isinstance(repo, str) or len(repo.split("/")) != 2
                            or not all(repo.split("/")) or type(pr) is not int or pr <= 0):
                        return {"status": "unknown"}
                    result.update(repo=repo, pr=pr)
                return result
        elif status == "retry":
            due = receipt.get("next_retry")
            if type(due) in (int, float) and math.isfinite(due) and due > self.clock():
                return {"status": "retry", "next_retry": due}
        return {"status": "unknown"}

    def send(self, delivery_id: str, destination: str, body: dict) -> dict:
        try:
            sent = self.backend.send(delivery_id, destination, body)
        except Exception:
            # User lookup does not bind the body; other backends can reconcile.
            return {"status": "unknown"} if destination == "user" else self.reconcile(delivery_id, destination)
        if destination == "user":
            if (isinstance(sent, dict) and sent.get("delivery_id") == delivery_id and
                    sent.get("destination") == destination and sent.get("status") == "unknown" and
                    sent.get("accepted") is True):
                accepted_at = sent.get("accepted_at")
                if type(accepted_at) in (int, float) and math.isfinite(accepted_at) and 0 <= accepted_at <= self.clock():
                    return {"status": "unknown", "accepted": True, "accepted_at": accepted_at}
            return {"status": "unknown"}
        return self.reconcile(delivery_id, destination)
