"""Internal Python values. They do not define the unconfirmed CLI wire format."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from typing import Mapping, Protocol

ACTIONS = frozenset(("publish", "reply", "respond", "friend_request", "friend_accept", "feed_feedback"))
PERMISSION_ACTIONS = ACTIONS - {"feed_feedback"}
URL_PREFIXES = tuple(x.strip() for x in os.environ.get(
    "AX_SCHEDULED_PUBLISHER_URL_PREFIXES",
    "https://github.com/example-owner/,https://example.invalid/",
).split(",") if x.strip())
SEARCH_TERMS = (
    "launch results upvotes stars", "Show HN launch", "Reddit launch side project",
    "indie developer X followers growth", "work log time tracking AI agent",
)
REQUIRES = ("linux-sandbox", "scheduled-publisher-cli")


@dataclass(frozen=True)
class Candidate:
    action: str
    text: str = ""
    item_id: str = ""
    conv_id: str = ""
    target: str = ""
    note: str = ""
    url: str = ""

    def payload(self):
        return asdict(self)

    @property
    def body(self):
        return self.note if self.action == "friend_request" else self.text

    @property
    def subject(self):
        return self.conv_id if self.action == "reply" else self.item_id or self.target


@dataclass(frozen=True)
class Decision:
    report: str
    candidates: tuple[Candidate, ...]
    reported_item_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvResult:
    state: str
    env_id: str = ""
    output: Mapping = field(default_factory=dict, repr=False)
    definitely_not_sent: bool = False


@dataclass(frozen=True)
class Settings:
    collect_budget_min: int = 15
    decide_budget_min: int = 40
    send_budget_min: int = 10
    run_send_max: int = 12
    reply_cap_day: int = 5
    repeat_failure: int = 3

    def __post_init__(self):
        for name, lo, hi in (("collect_budget_min", 5, 40), ("decide_budget_min", 10, 50),
                             ("send_budget_min", 2, 30), ("run_send_max", 1, 100),
                             ("reply_cap_day", 0, 50), ("repeat_failure", 1, 100)):
            value = getattr(self, name)
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"invalid {name}")


class EnvironmentRunner(Protocol):
    def select(self, *, requires: list[str], sensitivity: str): ...
    def run(self, request: dict, artifacts: Mapping[str, bytes]) -> EnvResult: ...


class Worker(Protocol):
    def decide(self, *, role: str, budget_min: int, sensitivity: str, inputs: dict) -> Decision: ...


class UnconfirmedCLI:
    """Production must remain closed until B18 confirms entry schemas and receipts."""

    def collect_args(self):
        raise NotImplementedError("SPEC S-23.7 / D-23.2: read-allowlist registry and CLI help unconfirmed")

    def send_args(self, candidate: Candidate):
        raise NotImplementedError("SPEC S-23.7 / D-23.2: fixed CLI argv and approved row schema unconfirmed")

    def receipt(self, result: EnvResult):
        raise NotImplementedError("SPEC S-23.4 / F-11.9: external result lookup unconfirmed")

    def lookup(self, env_id, record):
        raise NotImplementedError("SPEC S-23.7: lookup must use the current identity owner")
