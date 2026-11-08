"""Load F-11.8 targets and attach the scope from the B16b-1 registry."""
from pathlib import Path

import yaml

from .engine import Target


def load_targets(path, registry):
    body = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    records = body.get("targets") if isinstance(body, dict) and set(body) == {"targets"} else body
    if not isinstance(records, list):
        raise ValueError("invalid_discord_targets")
    required = {"id", "label", "x_user", "secret", "channel_id", "include_retweets", "include_replies"}
    targets = []
    for record in records:
        if not isinstance(record, dict) or set(record) != required:
            raise ValueError("invalid_discord_target_fields")
        scopes = [t for t in registry.for_plugin("discord-forward")
                  if t["destination"] == record["channel_id"] and "送信" in t["operations"]]
        if len(scopes) != 1:
            raise ValueError("discord_target_scope_not_unique")
        targets.append(Target(**record, project=scopes[0]["project"], repo=scopes[0]["repo"]))
    if len({target.id for target in targets}) != len(targets):
        raise ValueError("duplicate_target_id")
    return targets
