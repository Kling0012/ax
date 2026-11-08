"""S-23.2/S-23.4 forwarding with an AX-only durable queue."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from axcommon import timeutil
from core.intake.dest.b16b.delivery import DeliveryJournal


@dataclass(frozen=True)
class Post:
    id: str
    created_at: str
    text: str
    retweet: bool = False
    reply: bool = False

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[0-9]+", self.id):
            raise ValueError("invalid_post_id")
        timeutil.parse_ts(self.created_at)
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("invalid_post_text")
        if type(self.retweet) is not bool or type(self.reply) is not bool:
            raise ValueError("invalid_post_flags")


@dataclass(frozen=True)
class Target:
    id: str
    label: str
    x_user: str
    secret: str
    channel_id: str
    project: str
    repo: str
    include_retweets: bool = False
    include_replies: bool = False

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", self.id):
            raise ValueError("invalid_target_id")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", self.x_user):
            raise ValueError("invalid_x_user")
        if not re.fullmatch(r"[0-9]+", self.channel_id):
            raise ValueError("invalid_channel_id")
        if self.secret != f"discord-webhook-{self.id}":
            raise ValueError("invalid_secret_name")
        if self.include_replies is not False or type(self.include_retweets) is not bool:
            raise ValueError("invalid_target_flags")
        if any(not isinstance(s, str) or not s.strip() or s == "*"
               for s in (self.project, self.repo)):
            raise ValueError("invalid_target_scope")

    def delivery_target(self):
        return {"plugin_id": "discord-forward", "project": self.project,
                "repo": self.repo, "destination": self.channel_id,
                "operations": ["送信"]}


class DiscordForwarder:
    """One instance per graph lifetime, not per poll; restart resets started_at.

    PermissionReader uses the control API; registry and mode come from B16b-1.
    emit/lost are injected timeline/ledger writers and never receive post text.
    """

    def __init__(self, state_dir, targets, registry, permissions, source, webhook,
                 *, secret_scan, emit, lost, clock=timeutil.now,
                 start_delay_s=120, interval_s=300):
        if (type(start_delay_s) is not int or start_delay_s < 0
                or type(interval_s) is not int or interval_s < 1):
            raise ValueError("invalid_schedule")
        self.targets = tuple(targets)
        if len({t.id for t in self.targets}) != len(self.targets):
            raise ValueError("duplicate_target_id")
        self.registry, self.permissions = registry, permissions
        self.source, self.webhook, self.secret_scan = source, webhook, secret_scan
        self.clock, self.emit = clock, emit
        self.started_at = clock()
        self.next_poll = self.started_at.timestamp() + start_delay_s
        self.interval_s = interval_s
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / "forward.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS candidates (
                destination TEXT NOT NULL, source_item_id TEXT NOT NULL,
                target_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(destination, source_item_id))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS cursors (
                destination TEXT PRIMARY KEY, source_item_id TEXT NOT NULL)""")
        self.journal = DeliveryJournal(root / "delivery.sqlite", registry, emit=emit, lost=lost)
        self.journal.recover()

    def close(self):
        self.journal.close()
        self.db.close()

    def _event(self, kind, data):
        try:
            self.emit(kind, "discord-forward", data)
        except Exception:
            pass

    def targets_view(self):
        return [t.delivery_target() for t in self.targets]

    def _listed(self, target):
        return self.registry.contains(target.delivery_target(), "送信")

    def collect(self, target):
        if target not in self.targets or not self._listed(target):
            return {"state": "denied", "reason": "target_not_listed"}
        try:
            posts = list(self.source.fetch(target))
            # Reject a malformed batch before persisting any candidate.
            if any(not isinstance(post, Post) for post in posts):
                raise ValueError("invalid_posts")
        except Exception:
            self._event("automation.failed", {"target_id": target.id, "reason": "source_unavailable"})
            return {"state": "failed", "reason": "source_unavailable"}
        count = 0
        with self.lock, self.db:
            for post in posts:
                if (timeutil.parse_ts(post.created_at) <= self.started_at or post.reply
                        or (post.retweet and not target.include_retweets)):
                    continue
                result = self.db.execute("INSERT OR IGNORE INTO candidates VALUES (?, ?, ?, ?)",
                                         (target.channel_id, post.id, target.id,
                                          json.dumps(asdict(post))))
                count += result.rowcount
        return {"state": "collected", "count": count}

    @staticmethod
    def content(target, post):
        return f"{post.text}\nhttps://x.com/{target.x_user}/status/{post.id}"

    def _preflight(self, target, post):
        content = self.content(target, post)
        if len(content) > 2000 or self.secret_scan(content) is not True:
            return False
        if self.webhook.channel_matches(target) is not True:
            return False
        # Read again after channel GET to catch revocation during preflight.
        return self.permissions.check(target.delivery_target(), "送信") is True

    def send_pending(self, target, *, mode="collect_only"):
        if mode != "run":
            return []
        if target not in self.targets or not self._listed(target):
            return [{"state": "denied", "reason": "target_not_listed"}]
        with self.lock:
            rows = list(self.db.execute("SELECT * FROM candidates WHERE destination=? AND target_id=?",
                                        (target.channel_id, target.id)))
        results = []
        for row in sorted(rows, key=lambda r: int(r["source_item_id"])):
            post = Post(**json.loads(row["payload"]))
            previous = self.journal.get("discord-forward", target.channel_id, post.id)
            # Retain only attempted failures across restarts, not collected backlog.
            if timeutil.parse_ts(post.created_at) <= self.started_at:
                if previous is None or previous["state"] != "failed":
                    continue
            result = self.journal.deliver(
                mode=mode, target=target.delivery_target(), operation="送信",
                source_item_id=post.id, registered=True,
                expected_destination=target.channel_id, permission_reader=self.permissions,
                preflight=lambda: self._preflight(target, post),
                send=lambda: self.webhook.send(target, self.content(target, post)))
            results.append(result)
            if result["state"] == "sent":
                with self.lock, self.db:
                    cursor = self.db.execute("SELECT source_item_id FROM cursors WHERE destination=?",
                                             (target.channel_id,)).fetchone()
                    if cursor is None or int(post.id) > int(cursor[0]):
                        self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?, ?)",
                                        (target.channel_id, post.id))
        return results

    def cursor(self, target):
        with self.lock:
            row = self.db.execute("SELECT source_item_id FROM cursors WHERE destination=?",
                                  (target.channel_id,)).fetchone()
        return row[0] if row else None

    def tick(self, *, mode="collect_only"):
        if mode not in ("run", "collect_only") or self.clock().timestamp() < self.next_poll:
            return []
        self.next_poll = self.clock().timestamp() + self.interval_s
        self._event("automation.targets", {"targets": self.targets_view()})
        results = []
        for target in self.targets:
            try:
                results.append({"target_id": target.id, "collect": self.collect(target),
                                "deliveries": self.send_pending(target, mode=mode)})
            except Exception:
                # A broken dependency or target cannot stop other targets.
                results.append({"target_id": target.id, "state": "failed", "reason": "target_unavailable"})
                self._event("automation.failed", {"target_id": target.id, "reason": "target_unavailable"})
        return results
