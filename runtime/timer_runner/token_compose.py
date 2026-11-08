"""Build a Compose overlay for the credentials of registered timers."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml


_ID = re.compile(r"[a-z][a-z0-9-]*\Z")


def overlay(registry: Path, secrets_dir: Path) -> dict:
    data = yaml.safe_load(registry.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise ValueError("invalid plugin registry")
    ids = []
    scoped_ids = []
    for plugin in data["plugins"]:
        if not isinstance(plugin, dict):
            raise ValueError("invalid plugin entry")
        if plugin.get("kind") != "timer":
            continue
        pid = plugin.get("id")
        if not isinstance(pid, str) or not _ID.fullmatch(pid) or pid in ids:
            raise ValueError("invalid or duplicate timer id")
        ids.append(pid)
        manifest_path = plugin.get("path")
        if manifest_path:
            root = registry.resolve().parent.parent
            path = (root / manifest_path).resolve()
            if not path.is_relative_to(root):
                raise ValueError("invalid manifest path")
            manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("id") != pid:
                raise ValueError("invalid timer manifest")
            scopes = manifest.get("memory_capabilities")
            if scopes is not None:
                if (not isinstance(scopes, list) or any(scope not in ("memory.read", "memory.write") for scope in scopes)
                        or len(scopes) != len(set(scopes))):
                    raise ValueError("unsupported memory capabilities")
                if scopes == ["memory.read"]:
                    scoped_ids.append("memory-read--" + pid)
                elif scopes == ["memory.write"]:
                    scoped_ids.append("memory-write--" + pid)
    if set(ids) & set(scoped_ids):
        raise ValueError("credential name collision")
    ids.extend(scoped_ids)
    if not secrets_dir.is_absolute():
        raise ValueError("secrets directory must be absolute")
    return {
        "services": {"timer-runner": {"secrets": [f"plugin-token-{pid}" for pid in ids]}},
        "secrets": {f"plugin-token-{pid}": {"file": str(secrets_dir / "plugin-tokens" / pid)} for pid in ids},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("registry", type=Path)
    parser.add_argument("secrets_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(overlay(args.registry, args.secrets_dir)))


if __name__ == "__main__":
    main()
