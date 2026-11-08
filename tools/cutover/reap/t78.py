"""Run the copy-only T-78 check from the source host."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import yaml
from axcommon import timeutil
from core.archiver.config import load_dest, parse_sources
from core.archiver.guard import Guard
from core.archiver.manifest import matches_any, stat_state
from core.archiver.rsync import Transport
from tools.cutover.common import Authority, CommandRunner, Context, Profile
from tools.cutover.reap.common_adapter import from_context


# Only fingerprints and key options leave the destination. Key material stays there.
KEY_OPTIONS_SCRIPT = r'''set -eu
file=$1
fingerprint=$2
fps=$(ssh-keygen -lf "$file") || exit 1
awk -v wanted="$fingerprint" -v fingerprints="$fps" '
  BEGIN { count=split(fingerprints, lines, "\n"); for (i=1; i<=count; i++) { split(lines[i], fields, " "); keys[i]=fields[2] } }
  /^[[:space:]]*#/ || NF == 0 {next}
  {
    type=0
    for (i=1; i<=NF; i++) if ($i ~ /^(ssh-|ecdsa-|sk-)/) {type=i; break}
    if (!type) {bad=1; next}
    n++
    if (keys[n] == wanted) {
      options=""
      for (j=1; j<type; j++) options=options (j == 1 ? "" : " ") $j
      matches++
      selected=(type == 1 ? "unrestricted" : options)
    }
  }
  END {if (!bad && n == count && matches == 1) print selected; else exit 1}
' "$file"
'''


def key_restricted(profile: dict) -> str:
    """Fail closed unless exactly one matching public key confines the destination."""
    try:
        public_key = str(Path(profile["public_key"]).expanduser())
        result = subprocess.run(["ssh-keygen", "-lf", public_key], capture_output=True, text=True, timeout=15, check=True)
        fingerprint = result.stdout.split()[1]
        if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]+", fingerprint):
            return "unknown"
        host = profile["key_check_host"]
        auth = profile["authorized_keys"]
        destination = profile["restricted_directory"]
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", host) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", auth):
            return "unknown"
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", destination) or ".." in Path(destination).parts:
            return "unknown"
        command = "sh -s -- " + shlex.quote(auth) + " " + shlex.quote(fingerprint)
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                                input=KEY_OPTIONS_SCRIPT, capture_output=True, text=True, timeout=20, check=True)
        lines = result.stdout.splitlines()
        if len(lines) != 1:
            return "unknown"
        # Permit other SSH restrictions, but require an unambiguous forced rrsync.
        options = re.findall(r'(?:[^,\"]|\"[^\"]*\")+', lines[0])
        if ",".join(options) != lines[0]:
            return "unknown"
        if "restrict" not in options:
            return "missing"
        commands = [x for x in options if x.startswith("command=")]
        if len(commands) != 1 or not re.fullmatch(r'command="[^"\\]*"', commands[0]):
            return "missing"
        args = shlex.split(commands[0][len('command="'):-1])
        return ("ok" if len(args) == 2 and args[0] in ("rrsync", "/usr/bin/rrsync")
                and Path(args[1]) == Path(destination) else "missing")
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        return "unknown"


def select_file(profile, source_file, guard):
    doc = yaml.safe_load(source_file.read_text(encoding="utf-8"))
    sources = [s for s in parse_sources(doc) if s.name == "claude-projects"]
    if len(sources) != 1 or sources[0].remove_original:
        raise ValueError("copy_only_source_required")
    source = sources[0]
    root = Path(source.path).expanduser()
    guard.check_source_root(root)
    if (profile.ax_state_dir / "archive" / "reap-enabled").exists():
        raise ValueError("reap_marker_present")
    cutoff = (timeutil.now().timestamp() - max(7, source.min_age_days) * 86400) * 1e9
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(x for x in names if not (Path(directory) / x).is_symlink() and x not in guard.names)
        for name in sorted(files):
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if source.include and not matches_any(rel, source.include):
                continue
            if matches_any(rel, source.exclude) or not guard.allowed(root, path):
                continue
            state = stat_state(root, rel)
            if state and state.mtime_ns < cutoff:
                return rel
    raise ValueError("old_probe_file_missing")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tools.cutover.reap.t78")
    parser.add_argument("--profile", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--go-file", type=Path)
    parser.add_argument("--approval-id")
    parser.add_argument("--build-go", type=Path)
    args = parser.parse_args(argv)
    operator = None
    try:
        conf = yaml.safe_load(args.profile.read_text(encoding="utf-8"))
        profile_file = Path(conf["ax_profile"]).expanduser()
        profile = Profile.load(profile_file)
        sources = Path(conf["sources_file"]).expanduser()
        dest = load_dest(profile_file, environ={})
        archive = Path(conf["cutover_archive"]).expanduser()
        changelog = Path(conf["changelog"]).expanduser()
        guard = Guard.from_environment({"AX_SECRETS_DIR_HOST": str(Path(conf["secrets_dir"]).expanduser())})
        authority = None
        if args.apply:
            if not args.go_file or not args.approval_id:
                raise ValueError("go_and_approval_required")
            authority = Authority(args.go_file, args.approval_id, args.build_go)
        ctx = Context(profile, archive, CommandRunner(dry_run=args.dry_run), args.dry_run, authority)
        operator = from_context(ctx, sources_file=sources, changelog=changelog, restart_argv=["true"])
        relative = select_file(profile, sources, guard)
        destination = f"claude-projects/{timeutil.now_ts()[:10]}"
        if args.dry_run:
            print(json.dumps({"result": "planned", "relative_path": relative,
                              "destination": f"{dest.user}@{dest.host}:{destination}/",
                              "verification": "rsync --checksum; --dry-run --itemize-changes"}))
            return 0
        restriction = key_restricted(conf)
        if restriction != "ok":
            reason = "restricted_key_" + restriction
            recorded = operator._note("T-78", "failed", reason)
            print(json.dumps({"result": "failed", "reason": reason, "recorded": recorded}))
            return 4
        transport = Transport(dest, key=str(Path(conf["private_key"]).expanduser()),
                              known_hosts=str(Path(conf["known_hosts"]).expanduser()))
        outcome = operator.verify_t78(transport, relative_path=relative, guard=guard)
        print(json.dumps({"result": outcome.result, "reason": outcome.reason, "recorded": outcome.recorded}))
        return 0 if outcome.result == "ok" else 4
    except Exception:
        recorded = False
        if args.apply and operator is not None:
            recorded = operator._note("T-78", "failed", "t78_precondition_failed")
        print(json.dumps({"result": "failed", "reason": "t78_precondition_failed", "recorded": recorded}))
        return 4


if __name__ == "__main__":
    sys.exit(main())
