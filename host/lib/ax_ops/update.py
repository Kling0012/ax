"""CLI adapter for the four ledger update destinations."""
from __future__ import annotations

import json

DESTINATIONS = {"goal": "goal.update", "commitment": "commitment.update", "life": "life_state.update", "schedule": "schedule.update"}


def add_parser(commands):
    parser = commands.add_parser("update", help="Update ledger records")
    parser.add_argument("kind", choices=tuple(DESTINATIONS))
    parser.add_argument("payload", help="JSON object matching the destination schema")
    return parser


def build_payload(args):
    payload = json.loads(args.payload)
    if not isinstance(payload, dict):
        raise ValueError("update payload must be a JSON object")
    return DESTINATIONS[args.kind], payload
