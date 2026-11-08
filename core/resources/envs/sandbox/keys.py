"""Compare slot values without exposing them to the caller or audit sink."""
import hashlib
import hmac
from collections.abc import Callable, Mapping


class DuplicateKey(ValueError):
    pass


def check_distinct(candidate_slot: str, candidate: bytes, slots: Mapping[str, bytes],
                   audit: Callable[[dict], None]) -> None:
    if not isinstance(candidate, bytes) or not candidate:
        raise ValueError("invalid_key")
    candidate_digest = hashlib.sha256(candidate).digest()
    duplicate = False
    for name, value in slots.items():
        if name == candidate_slot:
            continue
        if not isinstance(value, bytes) or not value:
            raise ValueError("invalid_key")
        match = hmac.compare_digest(candidate_digest, hashlib.sha256(value).digest())
        audit({"slot": name, "match": match})
        duplicate |= match
    if duplicate:
        raise DuplicateKey("slot_duplicate_value")
