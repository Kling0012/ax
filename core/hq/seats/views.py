"""Compatibility exports for the current HQ seat presentation API."""
from __future__ import annotations

from .render import completed_page, confirmation_page, seat_page
from .service import FACTOR_OPERATIONS, OPERATIONS, RegistrationDisplay, SeatService

# These aliases retain imports from the former module without keeping its
# incompatible rendering and CSRF implementation alive.
ACTIONS = OPERATIONS
FACTOR_ACTIONS = FACTOR_OPERATIONS

__all__ = [
    "ACTIONS",
    "FACTOR_ACTIONS",
    "OPERATIONS",
    "FACTOR_OPERATIONS",
    "RegistrationDisplay",
    "SeatService",
    "seat_page",
    "confirmation_page",
    "completed_page",
]
