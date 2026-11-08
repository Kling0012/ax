"""Fail-closed outbound guard for sandbox gate provider calls."""


class OutboundDenied(Exception):
    """A provider call was rejected before transport."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def guard_outbound(invoke, *, classify, scan_outbound, record_denial):
    """Wrap a G-path transport with trusted classification, scanning, and auditing.

    classify receives (env_id, mapping, args), including the actual payload, and
    must affirm its provenance as 'public'.  It must not trust worker assertions.
    record_denial receives only an environment ID and a fixed reason code.
    """
    def guarded(env_id, mapping, args):
        try:
            visibility = classify(env_id, mapping, args)
        except Exception:
            visibility = None
        if visibility != 'public':
            reason = 'repo_not_public'
        else:
            try:
                safe = scan_outbound(args) is True
            except Exception:
                safe = False
            reason = None if safe else 'scan_blocked'
        if reason is not None:
            try:
                record_denial(env_id, reason)
            except Exception:
                raise OutboundDenied('audit_failed') from None
            raise OutboundDenied(reason)
        return invoke(env_id, mapping, args)
    return guarded
