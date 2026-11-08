"""S-6.21 declaration checks. Transport authority never comes from payload."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.intake.vias import Ident

SENSITIVITY = ("public", "private", "sensitive")


class Rejected(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass(frozen=True)
class Decision:
    tier: str
    created_by: str
    quote: str | None
    hq: bool


def declaration(spec: Any) -> dict:
    if not isinstance(spec, dict) or not {"secrets", "outputs", "sensitivity"} <= spec.keys():
        raise Rejected(400, "spec_declaration_required")
    for key in ("secrets", "outputs"):
        value = spec[key]
        if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
            raise Rejected(400, "spec_declaration_required")
    if spec["sensitivity"] not in SENSITIVITY:
        raise Rejected(400, "spec_declaration_required")
    return {k: spec[k] for k in ("secrets", "outputs", "sensitivity")}


def internal_only(spec: dict) -> bool:
    return not spec["secrets"] and not spec["outputs"] and spec["sensitivity"] == "public"


def decide(spec: Any, quote: Any, ident: Ident) -> Decision:
    if not isinstance(ident, Ident):
        raise Rejected(403, "receptacle_not_allowed")
    hq = ident.via == "hq" and ident.from_ == "hq" and ident.principal == "user"
    if not hq and (ident.via not in {"local", "ssh", "tailnet"} or ident.from_ != "commander"):
        raise Rejected(403, "receptacle_not_allowed")
    if ident.max_tier not in {"operate", "irreversible"}:
        raise Rejected(403, "tier_insufficient")
    spec = declaration(spec)
    if not hq and (not isinstance(quote, str) or not 4 <= len(quote) <= 2000):
        raise Rejected(400, "quote_required")
    if hq and quote is not None and not isinstance(quote, str):
        raise Rejected(400, "quote_required")
    # S-6.21 explicitly permits operate seats to request approval for this destination.
    return Decision("operate" if internal_only(spec) else "irreversible",
                    "user" if hq else ident.principal, quote, hq)


def within_declaration(manifest: Any, spec: dict, tier: str) -> bool:
    try:
        actual = declaration(manifest)
        allowed = declaration(spec)
    except Rejected:
        return False
    return (set(actual["secrets"]) <= set(allowed["secrets"])
            and set(actual["outputs"]) <= set(allowed["outputs"])
            and SENSITIVITY.index(actual["sensitivity"]) <= SENSITIVITY.index(allowed["sensitivity"])
            and (tier != "operate" or internal_only(actual)))
