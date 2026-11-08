"""席の唯一の書き手と最終正常版（S-6.8、D-5.1）。"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import struct
import threading
from typing import Callable

import yaml

from axcommon import ids, timeutil

SEAT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
TIERS = {"read", "operate", "irreversible"}
STATES = {"active", "suspended", "revoked"}
FIELDS = {"seat_id", "label", "via", "device", "device_id", "public_key", "key_fp",
          "token_digest", "tier", "state", "created_at", "created_by", "expires_at",
          "last_seen_at", "revoked_at", "revoked_by", "note"}


class SeatError(Exception):
    def __init__(self, reason: str, status: int = 403):
        super().__init__(reason)
        self.reason, self.status = reason, status


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def key_fingerprint(public_key: str) -> str:
    parts = public_key.split()
    if len(parts) < 2 or parts[0] not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}:
        raise ValueError("invalid public key")
    if "\n" in public_key or "\r" in public_key or "\x00" in public_key:
        raise ValueError("invalid public key")
    raw = base64.b64decode(parts[1], validate=True)
    if len(raw) < 4:
        raise ValueError("invalid key blob")
    size = struct.unpack(">I", raw[:4])[0]
    if raw[4:4 + size].decode("ascii") != parts[0] or len(raw) <= 4 + size:
        raise ValueError("invalid key algorithm")
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def validate_row(row: dict) -> dict:
    if not isinstance(row, dict) or set(row) - FIELDS:
        raise ValueError("invalid seat fields")
    row = copy.deepcopy(row)
    required = {"seat_id", "label", "via", "device", "device_id", "created_at", "created_by", "expires_at"}
    if not required <= set(row):
        raise ValueError("missing seat fields")
    if not isinstance(row["seat_id"], str) or not SEAT_ID.fullmatch(row["seat_id"]):
        raise ValueError("invalid seat id")
    for key in ("label", "device", "device_id", "created_by"):
        if not isinstance(row[key], str) or not row[key]:
            raise ValueError("invalid seat value")
    row.setdefault("tier", "read")
    row.setdefault("state", "active")
    if row["tier"] not in TIERS or row["state"] not in STATES:
        raise ValueError("invalid seat tier or state")
    for key in ("created_at", "expires_at", "last_seen_at", "revoked_at"):
        if row.get(key) is not None:
            timeutil.parse_ts(row[key])
    if timeutil.parse_ts(row["expires_at"]) <= timeutil.parse_ts(row["created_at"]):
        raise ValueError("invalid expiry")
    if row["via"] == "ssh":
        if "token_digest" in row or key_fingerprint(row["public_key"]) != row.get("key_fp"):
            raise ValueError("invalid SSH credential")
    elif row["via"] == "tailnet":
        if "public_key" in row or "key_fp" in row or not HEX_DIGEST.fullmatch(row.get("token_digest", "")):
            raise ValueError("invalid tailnet credential")
    else:
        raise ValueError("invalid seat via")
    return row


def encode_rows(rows: list[dict]) -> bytes:
    return yaml.safe_dump({"seats": rows}, allow_unicode=True, sort_keys=True).encode()


def decode_rows(raw: bytes) -> dict[str, dict]:
    obj = yaml.safe_load(raw)
    if not isinstance(obj, dict) or set(obj) != {"seats"} or not isinstance(obj["seats"], list):
        raise ValueError("invalid seats document")
    result = {}
    credentials = set()
    for row in obj["seats"]:
        row = validate_row(row)
        credential = (row["via"], row.get("key_fp", row.get("token_digest")))
        if row["seat_id"] in result or credential in credentials:
            raise ValueError("duplicate seat credential or id")
        result[row["seat_id"]] = row
        credentials.add(credential)
    return result


class SeatStore:
    """台帳と intake 所有の控えを管理する。外部の版を自動採用しない。"""

    def __init__(self, seats_file: Path | str, database: Path | str, *, archive_dir: Path | str,
                 alert: Callable[[str], None] | None = None):
        self.path, self.archive = Path(seats_file), Path(archive_dir)
        self.alert = alert or (lambda reason: None)
        self.lock = threading.RLock()
        self.rows: dict[str, dict] | None = None
        self.degraded = True
        self.database_ok = True
        self.db = None
        try:
            self.db = sqlite3.connect(str(database), check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS seat_versions (
                    version INTEGER PRIMARY KEY AUTOINCREMENT, digest TEXT NOT NULL,
                    content BLOB NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS enroll_codes (
                    code_digest TEXT PRIMARY KEY, seat_draft_json TEXT NOT NULL,
                    device_id TEXT, expires_at TEXT NOT NULL, used_at TEXT, fail_count INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS seat_policy (name TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS seat_enroll_failures (
                    device_id TEXT PRIMARY KEY, fail_count INTEGER NOT NULL, locked_until TEXT);
                CREATE TABLE IF NOT EXISTS seat_effects (
                    principal TEXT NOT NULL, reason TEXT NOT NULL, action TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(principal, reason, action));
            """)
            self.db.commit()
            self.refresh()
        except (sqlite3.Error, OSError, ValueError):
            self.database_ok = False
            self._alert("seat_store_unavailable")

    def _alert(self, reason: str) -> None:
        try:
            self.alert(reason)
        except Exception:
            pass

    def _database(self):
        if not self.database_ok or self.db is None:
            raise SeatError("unavailable", 503)
        return self.db

    def refresh(self) -> None:
        with self.lock:
            db = self._database()
            try:
                own = db.execute("SELECT digest,content FROM seat_versions ORDER BY version DESC LIMIT 1").fetchone()
                if own is None:
                    self.rows = None
                    self.degraded = True
                    self._alert("seats_no_trusted_version")
                    return
                raw = bytes(own["content"])
                if digest(raw) != own["digest"]:
                    raise ValueError("trusted version damaged")
                rows = decode_rows(raw)
                try:
                    st = self.path.stat()
                    current = self.path.read_bytes()
                    valid = (st.st_mode & 0o777) == 0o600 and st.st_uid == os.geteuid() and not self.path.is_symlink()
                    self.degraded = not (valid and digest(current) == own["digest"])
                except OSError:
                    self.degraded = True
                self.rows = rows
                if self.degraded:
                    self._alert("seats_integrity_mismatch")
            except (sqlite3.Error, ValueError, TypeError, KeyError, yaml.YAMLError):
                self.degraded = True
                self._alert("seats_unreadable")

    def initialize(self) -> None:
        """初回だけ、存在しない台帳を空で作る。既存ファイルは取り込まない。"""
        with self.lock:
            if self.rows is not None:
                return
            if self.path.exists() or self.path.is_symlink():
                raise SeatError("unavailable", 503)
            self._save({})

    def _save(self, rows: dict[str, dict], effects: list[tuple[str, str, str]] = ()) -> None:
        db = self._database()
        raw = encode_rows(list(rows.values()))
        decode_rows(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.archive.mkdir(parents=True, exist_ok=True)
        old = db.execute("SELECT content FROM seat_versions ORDER BY version DESC LIMIT 1").fetchone()
        if old is not None:
            archived = self.archive / (ids.new_ulid() + ".yaml")
            with archived.open("xb") as stream:
                os.chmod(archived, 0o600)
                stream.write(bytes(old["content"]))
                stream.flush()
                os.fsync(stream.fileno())
        with db:
            db.execute("INSERT INTO seat_versions(digest,content,created_at) VALUES(?,?,?)",
                       (digest(raw), raw, timeutil.now_ts()))
            db.executemany("INSERT OR IGNORE INTO seat_effects(principal,reason,action) VALUES(?,?,?)", effects)
        # 控えを先に確定する。入れ替えに失敗しても控えで失効を守る。
        self.rows = copy.deepcopy(rows)
        try:
            # 同じディレクトリの一時名から入れ替える。別マウントへのコピーを作らない。
            staging = self.path.parent / (".seats-" + ids.new_ulid())
            with staging.open("xb") as stream:
                os.chmod(staging, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, self.path)
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self.degraded = False
        except OSError:
            self.degraded = True
            self._alert("seats_publish_failed")

    def put(self, row: dict) -> dict:
        with self.lock:
            self.refresh()
            if self.rows is None:
                raise SeatError("unavailable", 503)
            row = validate_row(row)
            rows = copy.deepcopy(self.rows)
            rows[row["seat_id"]] = row
            self._save(rows)
            return copy.deepcopy(row)

    def get(self, seat_id: str) -> dict:
        with self.lock:
            self.refresh()
            if self.rows is None:
                raise SeatError("seat_unknown", 401)
            row = self.rows.get(seat_id)
            if row is None:
                raise SeatError("seat_unknown", 401)
            return copy.deepcopy(row)

    def by_token(self, token: str, device_id: str) -> dict:
        with self.lock:
            self.refresh()
            for row in (self.rows or {}).values():
                if row["via"] == "tailnet" and secrets.compare_digest(row["token_digest"], digest(token)):
                    if row["device_id"] != device_id:
                        raise SeatError("peer_unknown", 403)
                    return copy.deepcopy(row)
            raise SeatError("seat_unknown", 401)

    def policy(self, name: str, default=None):
        with self.lock:
            row = self._database().execute("SELECT value_json FROM seat_policy WHERE name=?", (name,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_policy(self, name: str, value) -> None:
        with self.lock, self._database():
            self.db.execute("INSERT INTO seat_policy VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json",
                            (name, json.dumps(value)))

    def transition(self, seat_id: str, *, state: str, actor: str, reason: str, actions: tuple[str, ...]) -> dict:
        with self.lock:
            row = self.get(seat_id)
            if row["state"] == "revoked" and state != "revoked":
                raise SeatError("seat_revoked", 401)
            row["state"] = state
            if state == "revoked":
                row.update(revoked_at=timeutil.now_ts(), revoked_by=actor)
            rows = copy.deepcopy(self.rows)
            rows[seat_id] = row
            self._save(rows, [(seat_id, reason, action) for action in actions])
            return row

    def pending_effects(self):
        with self.lock:
            return [dict(row) for row in self._database().execute("SELECT * FROM seat_effects WHERE done=0")]

    def finish_effect(self, principal: str, reason: str, action: str):
        with self.lock, self._database():
            self.db.execute("UPDATE seat_effects SET done=1 WHERE principal=? AND reason=? AND action=?", (principal, reason, action))

    def close(self):
        if self.db is not None:
            self.db.close()
