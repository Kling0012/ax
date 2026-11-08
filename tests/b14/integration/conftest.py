import pytest

from core.intake.dest.b12b3.change import ChangeLedger
from plugins.subloop.change.integration import ChangeFlow, Journal
from tests.fakes.b14.integration import Clock, HEAD, make_ports, target


@pytest.fixture
def harness(tmp_path):
    clock = Clock()
    fake = make_ports(clock)
    journal = Journal(tmp_path / "change.db", scrub=fake.ports.scrub, clock=clock)
    ledger = ChangeLedger(tmp_path / "operations.db", ["owner/issues"],
                          resolve_pr=lambda repo, number: {"issue_repo": repo, "number": number,
                                                           "head_sha": HEAD})
    for number in (1, 2):
        ledger.request(f"request-{number}", {"issue_repo": "owner/issues", "kind": "pr",
                                              "number": number, "dry_run": False}, "owner")
    journal.change_ledger = ledger
    flow = ChangeFlow(fake.ports, journal, [target()], clock=clock)
    fake.flow, fake.journal, fake.clock = flow, journal, clock
    yield fake
    journal.close()
