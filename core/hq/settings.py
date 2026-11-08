"""HQ runtime settings; no deployment or host mutation."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

import yaml

from .auth import AccessVerifier, Authenticator, fetch_jwks
from .secrets import NAME, SecretSlots


def _yaml(path: Path):
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("configuration_invalid")
    return value


def names_file(path: Path, *, optional=False) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        if optional:
            return set()
        raise
    if len(lines) > 1000 or any(len(line) > 320 for line in lines):
        raise ValueError("users_invalid")
    values = {line.strip() for line in lines if line.strip()}
    if any("@" not in value or any(c.isspace() for c in value) for value in values):
        raise ValueError("users_invalid")
    return values


def secret_path(root: Path, filename: str) -> Path:
    if not isinstance(filename, str) or not NAME.fullmatch(filename):
        raise ValueError("secret_filename_invalid")
    return root / filename


@dataclass
class Settings:
    public_proxy_ip: str = ""
    issuer: str = ""
    jwks_url: str = ""
    aud_file: Path = Path("/run/secrets/hq-access")
    users_file: Path = Path("/run/secrets/hq-allowed-users")
    email_slot: str = "hq-access-email"
    slots_dir: Path = Path("/secrets-in")
    slots: dict = field(default_factory=dict)
    origins: frozenset[str] = frozenset()
    sample: bool = False
    ui_dir: Path = Path(__file__).parent / "ui"
    socket_path: Path = Path("/run/hq/hq.sock")
    # Resource registry adapter: absence denies remote inputs.
    secret_target_registered: object = None

    @classmethod
    def load(cls):
        config = Path(os.environ.get("AX_CONFIG_DIR", "/ax/config"))
        # The adapter takes the profile's hq_access object without renaming its keys.
        path = Path(os.environ.get("AX_HQ_ACCESS_CONFIG", str(config / "hq-access.yaml")))
        raw = _yaml(path)
        access = raw["hq_access"]
        users = _yaml(config / "hq-allowed-users.yaml")["file"]
        root = Path("/run/secrets")
        if users != access["allowed_users_file"]:
            raise ValueError("users_filename_mismatch")
        slots = _yaml(config / "secret-slots.yaml")["slots"]
        if not isinstance(slots, dict):
            raise ValueError("slots_invalid")
        email_slot = access["email_slot"]
        if not NAME.fullmatch(email_slot):
            raise ValueError("email_slot_invalid")
        return cls(public_proxy_ip=os.environ.get("AX_PUBLIC_PROXY_IP", raw.get("public_proxy_ip", "")),
                   issuer=access["iss"], jwks_url=access["jwks_url"],
                   aud_file=secret_path(root, access["aud_secret"]), users_file=secret_path(root, users),
                   email_slot=email_slot, slots=slots, origins=frozenset(raw.get("origins", [])),
                   sample=raw.get("sample", False) is True)

    def authenticator(self, *, missing_email=None, jwks=None):
        def audience():
            value = self.aud_file.read_text(encoding="utf-8").strip()
            if not value or len(value) > 1024 or any(c.isspace() for c in value):
                raise ValueError("aud_invalid")
            return value

        access = AccessVerifier(self.issuer, audience, jwks or (lambda: fetch_jwks(self.jwks_url)))
        return Authenticator(self.public_proxy_ip, lambda: names_file(self.users_file), access,
                             lambda: names_file(self.slots_dir / self.email_slot, optional=True), missing_email)

    def secret_slots(self, *, emit=None, intake=None):
        return SecretSlots(self.slots_dir, self.slots, registered=self.secret_target_registered, emit=emit, intake=intake)
