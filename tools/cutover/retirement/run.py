"""Produce a simulated, non-operational retirement plan."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import shlex

from . import CommandResult, Context, Host, PRECONDITIONS, Profile
from .common_adapter import RetirementSession
from .engine import COMPUTE_ROLE

STEPS = tuple(f"G{i}" for i in range(1, 12))


def plan(host: str, requested: str) -> dict:
    if requested == "t142":
        return {"status": "blocked", "reason": "ax-ci-runner not installed"}

    # These are placeholders, not discovered installation values. An arbitrary
    # host is modeled as local; the named roles select their corresponding flow.
    role = host if host in ("local", "legacy", COMPUTE_ROLE) else "local"
    names = {r: host if r == role else f"simulation-{r}" for r in ("local", "legacy", COMPUTE_ROLE)}
    if len(set(names.values())) != 3:
        raise ValueError("host conflicts with a simulation role")

    def make_host(r: str) -> Host:
        home = f"/simulation/{r}/home"
        return Host(names[r], home, f"{home}/.config/systemd/user",
                    f"/simulation/{r}/etc/systemd/system", f"/simulation/{r}/usr/local/sbin",
                    f"/simulation/{r}/etc/fstab", f"/simulation/{r}/mnt/bind",
                    f"/simulation/{r}/run/user/1000", 1000,
                    f"/simulation/{r}/disk" if r == COMPUTE_ROLE else None)

    hosts = {r: make_host(r) for r in names}
    profile = Profile(hosts["local"], hosts["legacy"], hosts[COMPUTE_ROLE],
                      date.today().strftime("%Y%m%d"), {key: True for key in PRECONDITIONS})
    calls: dict[str, list[dict]] = {step: [] for step in STEPS}
    events: dict[str, dict] = {}
    current: list[str | None] = [None]

    def execute(target, argv, stdin):
        if current[0] and target == host:
            args = list(argv)
            if stdin is not None:
                args[-1] = "<retirement fsops program>"
            call = {"command": shlex.join(args)}
            if stdin is not None:
                call["input"] = json.loads(stdin)
            calls[current[0]].append(call)
        if stdin is not None:
            op = json.loads(stdin)["op"]
            data = {"busy": False, "paths": [], "components": []}
            return CommandResult(stdout=json.dumps(data))
        if "is-active" in argv:
            return CommandResult(stdout="inactive\n")
        if "is-enabled" in argv:
            return CommandResult(stdout="disabled\n")
        return CommandResult()

    def record(kind, subject, data):
        if kind == "cutover.state" and subject.startswith("gh-auto-old/"):
            step = subject.rsplit("/", 1)[-1]
            if data.get("target") == f"{step}/{host}":
                events[step] = {"status": data["result"]}
                if "reason" in data:
                    events[step]["reason"] = data["reason"]

    context = Context(execute, record, lambda _: None, lambda *_: None, lambda: None)
    session = RetirementSession.create(context, profile)
    original_step = session.executor.step

    def capture_step(target, step, action, **kwargs):
        current[0] = step
        try:
            return original_step(target, step, action, **kwargs)
        finally:
            current[0] = None

    session.executor.step = capture_step
    session.retire_others()
    result = []
    for step in (STEPS if requested == "all" else (requested,)):
        if step == "G9":
            outcome = {"status": "skipped", "reason": "t142"}
        else:
            outcome = events.get(step, {"status": "skipped", "reason": "not applicable to host role"})
        result.append({"step": step, **outcome,
                       "reversible": step in ("G2", "G9", "G11") or outcome["status"] == "skipped",
                       "backup": None, "commands": calls[step]})
    return {"host": host, "role": role, "simulation": "placeholder paths and synthetic state",
            "steps": result}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--step", required=True, choices=(*STEPS, "t142", "all"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--record", type=Path)
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.exit(2, "not implemented\n")
    try:
        output = json.dumps(plan(args.host, args.step), ensure_ascii=False, indent=2) + "\n"
        if args.record is not None:
            args.record.write_text(output, encoding="utf-8")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"{exc}\n")
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
