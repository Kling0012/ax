"""Operator CLI. It prints a plan unless --apply is explicitly selected."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import importlib
import json
from pathlib import Path
import sys

from .common import Authority, CommandRunner, Context, CutoverError, IntegrationPending, Profile
from .stages import Operator, PendingBackend


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ax-cutover")
    ap.add_argument("--profile", type=Path, required=True)
    ap.add_argument("--archive", type=Path, required=True)
    ap.add_argument("--stage", type=int, choices=range(1, 7), required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    ap.add_argument("--go-file", type=Path)
    ap.add_argument("--approval-id")
    ap.add_argument("--build-go", type=Path)
    ap.add_argument("--adapter", help="operator-reviewed local module:factory; called with Context")
    args = ap.parse_args(argv)
    try:
        profile = Profile.load(args.profile)
        if not args.archive.is_absolute():
            raise CutoverError("archive_must_be_absolute")
        authority = None
        if args.apply:
            if not args.go_file or not args.approval_id:
                raise CutoverError("go_and_approval_required")
            authority = Authority(args.go_file, args.approval_id, args.build_go)
        ctx = Context(profile, args.archive, CommandRunner(dry_run=not args.apply), not args.apply, authority)
        backend = PendingBackend()
        if args.apply:
            ctx.guard()
            if args.adapter:
                module, separator, factory = args.adapter.partition(":")
                if not separator or not module or not factory.isidentifier():
                    raise CutoverError("adapter_invalid")
                backend = getattr(importlib.import_module(module), factory)(ctx)
        outcome = Operator(ctx, backend).advance(args.stage)
        print(json.dumps(asdict(outcome), ensure_ascii=False))
        return 0 if outcome.result in ("ok", "planned") else 4
    except Exception:
        # Never print exception text from an adapter, profile or subprocess.
        print(json.dumps({"result": "blocked", "stage": args.stage,
                          "reason": "cutover_precondition_or_adapter_failed"}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
