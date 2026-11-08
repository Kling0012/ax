"""Exercise the change flow with a receipt-capable fake delivery service."""
from plugins.subloop.change.integration.deployment_delivery import DeploymentDelivery
from tests.fakes.b14.integration import item


class ReceiptService:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.receipt = None
        self.fail = False

    def send(self, delivery_id, destination, body):
        self.calls.append((delivery_id, destination))
        if self.fail:
            raise OSError("unconfirmed write")
        self.receipt = {"delivery_id": delivery_id, "destination": destination,
                        "status": "unknown", "accepted": True, "accepted_at": self.clock()}
        return self.receipt

    def lookup(self, delivery_id, destination):
        return self.receipt or {"delivery_id": delivery_id, "destination": destination,
                                "status": "unknown"}


def use_service(h):
    service = ReceiptService(h.clock)
    h.flow.notices.port = DeploymentDelivery(service, clock=h.clock)
    return service


def test_confirmed_notice_starts_wait_at_receipt_time(harness):
    h = harness
    service = use_service(h)
    request = item(trusted=False)
    h.permissions.authorize(request)
    assert h.flow.process(request, dry_run=False, mode="run")["reason"] == "merge_notice_wait"
    h.clock.advance(1799)
    assert h.flow.process(request, dry_run=False, mode="run")["reason"] == "merge_notice_wait"
    h.clock.advance(1)
    assert h.flow.process(request, dry_run=False, mode="run")["status"] == "succeeded"
    assert len(service.calls) == 1


def test_failed_send_stays_unknown_without_repeated_notification(harness):
    h = harness
    service = use_service(h)
    service.fail = True
    request = item(trusted=False)
    h.permissions.authorize(request)
    for _ in range(3):
        assert h.flow.process(request, dry_run=False, mode="run")["reason"] == "merge_notice_required"
        h.clock.advance(120)
    key, destination = service.calls[0]
    service.receipt = {"delivery_id": key, "destination": destination, "status": "absent"}
    h.clock.advance(120)
    assert h.flow.process(request, dry_run=False, mode="run")["reason"] == "merge_notice_required"
    assert len(service.calls) == 1
    assert not h.github.merges
    assert h.journal.operation(key)["state"] == "lost"


def test_wrong_receipt_and_future_time_cannot_start_wait(harness):
    h = harness
    service = use_service(h)
    service.fail = True
    request = item(trusted=False)
    h.permissions.authorize(request)
    h.flow.process(request, dry_run=False, mode="run")
    key, destination = service.calls[0]
    service.receipt = {"delivery_id": "other", "destination": destination,
                       "status": "delivered", "delivered_at": h.clock()}
    h.clock.advance(120)
    assert h.flow.process(request, dry_run=False, mode="run")["reason"] == "merge_notice_required"
    service.receipt.update(delivery_id=key, delivered_at=h.clock() + 999)
    h.clock.advance(120)
    assert h.flow.process(request, dry_run=False, mode="run")["reason"] == "merge_notice_required"
    assert len(service.calls) == 1
    assert not h.github.merges


def test_confirmed_pr_receipt_survives_lost_send_response(harness):
    service = ReceiptService(harness.clock)
    def send(delivery_id, destination, body):
        service.receipt = {"delivery_id": delivery_id, "destination": destination,
                           "status": "delivered", "delivered_at": harness.clock(),
                           "repo": "owner/code", "pr": 42}
        raise OSError("response lost")
    service.send = send
    port = DeploymentDelivery(service, clock=harness.clock)
    receipt = port.send("id", "pr", {})
    assert receipt == {"status": "delivered", "delivered_at": harness.clock(),
                       "repo": "owner/code", "pr": 42}
    assert harness.flow.adopt_publication({**item(), "pr": None}, receipt)["pr"] == 42
    service.receipt["pr"] = True
    assert port.reconcile("id", "pr")["status"] == "unknown"
    service.receipt.update(pr=42, delivery_id="other")
    assert port.reconcile("id", "pr")["status"] == "unknown"
