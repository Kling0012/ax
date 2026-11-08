"""Production seat listener and approval service registration."""
from __future__ import annotations

import os
from pathlib import Path

from axcommon import creds
from axcommon.client import ApiError, LedgerClient, service_url
from axcommon.defaults import Defaults
from axcommon.hq_proof import Verifier
from core.intake.approval import ApprovalService, ApprovalStore
from core.intake.approval.audit import ApprovalAudit
from core.intake.approval.integration import register as register_approval
from core.intake.approval.policy import AuthorityPolicy
from core.intake.seat import SeatService, SeatStore, register as register_seats
from core.intake.seat.store import SeatError


def _integer(name):
    value = os.environ.get(name, '')
    return int(value) if value.isdecimal() else None


def register(app):
    root = Path(os.environ.get('AX_STATE_DIR', '/state'))
    state = root / 'intake'
    state.mkdir(parents=True, exist_ok=True)
    verifier = Verifier(state / 'hq-nonces.sqlite')
    seats = SeatService(SeatStore(root / 'seat' / 'seats.yaml', state / 'seats.sqlite',
                                  archive_dir=root / 'archive' / 'seat'),
                        seat_uid=lambda: _integer('AX_SEAT_UID'), gate_address='',
                        gate_token=lambda: '', defaults=Defaults.load(), hq_verifier=verifier)

    def seat_check(ident):
        if ident.via in {'hq', 'local'}:
            return None
        try:
            row = seats.store.get(ident.principal)
            current = seats.authorize_row(row)
            if current.via != ident.via or current.max_tier != ident.max_tier:
                return 'seat_changed'
        except SeatError as error:
            return error.reason
        return None

    def ledger():
        token_path = os.environ.get('AX_CORE_TOKEN_FILE')
        token = Path(token_path).read_text().strip() if token_path else creds.read_secret_optional('core-token-intake')
        if not token:
            raise ValueError('ledger_credential_unavailable')
        return LedgerClient(service_url('ledger'), token=token, transport=app.state.intake.transport)

    def create_card(card):
        client = ledger()
        try:
            client.create_todo(card)
        except ApiError as error:
            if error.status != 409:
                raise
            existing = client.get_todo(card['id'])
            if (existing.get('card') or {}).get('approval_id') != card['id']:
                raise

    policy = AuthorityPolicy(seat_check=seat_check, todo_reader=lambda ident: ledger().get_todo(ident))
    audit = ApprovalAudit(sink=app.state.intake.sink, access=app.state.intake.audit)
    service = ApprovalService(ApprovalStore(state / 'approvals.sqlite'), policy=policy, audit=audit,
                              card_writer=create_card, ttl_min=Defaults.load().get('approval_ttl_min'))
    register_approval(app, service, hq_verifier=verifier)
    # Seat identity must be resolved before approval policy sees the request.
    register_seats(app, seats, seat_gid=_integer('AX_SEAT_GID'), hq_gid=_integer('AX_HQ_GID'),
                   hq_verifier=verifier)
