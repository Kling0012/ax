"""Reconcile notifications and release handoff using durable, stable IDs."""
from __future__ import annotations

from .journal import Journal


class Delivery:
    def __init__(self, journal: Journal, port, *, outbound_check, clock):
        self.journal, self.port = journal, port
        self.outbound_check, self.clock = outbound_check, clock

    def deliver(self, key: str, destination: str, body: dict) -> dict:
        if self.outbound_check(body) is not True:
            return {"status": "blocked", "reason": "outbound_check_failed"}
        op = self.journal.operation(key)
        if op and (op["body"].get("destination") != destination or op["body"].get("payload") != body):
            return {"status": "blocked", "reason": "delivery_identity_mismatch"}
        if op and op["state"] == "succeeded":
            return op["result"]
        if op and op["state"] == "failed":
            return op["result"]
        if op:
            if op["body"].get("destination") != destination or op["body"].get("payload") != body:
                return {"status": "blocked", "reason": "delivery_identity_mismatch"}
            lease = op["body"]["lease_until"]
            due = (op["result"] or {}).get("next_retry", lease)
            if self.clock() < max(lease, due):
                return {"status": "retry", "next_retry": max(lease, due)}
            try:
                observed = self.port.reconcile(key, destination)
            except Exception:
                observed = {"status": "unknown"}
            if self._confirmed(observed):
                self.journal.finish(key, "succeeded", observed)
                return observed
            if not isinstance(observed, dict) or observed.get("status") not in {"absent", "retry"}:
                self.journal.finish(key, "lost", {"status": "unknown", "next_retry": self.clock() + 60})
                return {"status": "unknown", "next_retry": self.clock() + 60}
            if observed.get("status") == "retry":
                due = observed.get("next_retry", self.clock() + 60)
                self.journal.finish(key, "retry_wait", {"status": "retry", "next_retry": due})
                return {"status": "retry", "next_retry": due}
            # Atomically take a new lease only after the destination has proved
            # absence. A second patrol cannot resend while this caller sends.
            with self.journal.lock:
                self.journal.db.execute("BEGIN IMMEDIATE")
                try:
                    current = self.journal.operation(key)
                    if current["state"] == "succeeded":
                        self.journal.db.commit()
                        return current["result"]
                    if current["body"]["lease_until"] > self.clock():
                        self.journal.db.commit()
                        return {"status": "retry", "next_retry": current["body"]["lease_until"]}
                    saved = {**current["body"], "lease_until": self.clock() + 120}
                    import json
                    self.journal.db.execute("UPDATE operation SET state='launching',body=? WHERE key=?", (json.dumps(saved), key))
                    self.journal.db.commit()
                except BaseException:
                    self.journal.db.rollback()
                    raise
        elif not self.journal.claim(key, "delivery", {"destination": destination, "payload": body}):
            return {"status": "retry", "next_retry": self.clock() + 120}
        try:
            result = self.port.send(key, destination, body)
        except Exception:
            result = {"status": "unknown"}
        if self._confirmed(result) or (destination == "user" and self._accepted(result)):
            self.journal.finish(key, "succeeded", result)
        elif isinstance(result, dict) and result.get("status") in {"absent", "retry"}:
            result = {**result, "next_retry": result.get("next_retry", self.clock() + 60)}
            self.journal.finish(key, "retry_wait", result)
        else:
            result = {"status": "unknown", "next_retry": self.clock() + 60}
            self.journal.finish(key, "lost", result)
        self.journal.event("change.delivery", key, {"destination": destination, **result})
        return result

    @staticmethod
    def _accepted(value) -> bool:
        import math
        return (isinstance(value, dict) and value.get("accepted") is True and
                type(value.get("accepted_at")) in {int, float} and math.isfinite(value["accepted_at"]))

    @staticmethod
    def _confirmed(value) -> bool:
        return isinstance(value, dict) and value.get("status") == "delivered" and type(value.get("delivered_at")) in {int, float}
