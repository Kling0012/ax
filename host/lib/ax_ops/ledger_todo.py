"""CLI parser and payload helpers for ToDo operations."""


def add_commands(commands):
    done = commands.add_parser("done", help="Complete a ToDo")
    done.add_argument("todo_id")
    done.add_argument("--reason")
    park = commands.add_parser("park", help="Park a ToDo")
    park.add_argument("todo_id")
    park.add_argument("--reason", required=True)
    park.add_argument("--check-at", required=True)


def request_payload(args):
    if not args.todo_id or not args.todo_id.strip():
        raise ValueError("todo_id is required")
    payload = {"todo_id": args.todo_id}
    if args.command == "done":
        if args.reason is not None:
            payload["reason"] = args.reason
        return "ledger.complete", payload
    if args.command == "park":
        if not args.reason or not args.check_at:
            raise ValueError("reason and check_at are required")
        payload.update(reason=args.reason, check_at=args.check_at)
        return "ledger.park", payload
    raise ValueError("unknown ToDo operation")
