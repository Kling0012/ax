"""CLI argument and payload mapping for the work.request destination."""

import re

ROLES = ("impl", "impl_hard", "summary", "research", "second_opinion", "screen", "assist")


def configure_parser(parser):
    parser.add_argument("--project", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--role", choices=ROLES)
    parser.add_argument("--body")
    parser.add_argument("--target")
    parser.add_argument("--revision")
    parser.add_argument("--attachment", action="append", dest="attachments")
    parser.add_argument("--example", action="append", dest="examples")
    parser.add_argument("--boundary", action="append", dest="boundaries")
    parser.add_argument("--verify")


def build_payload(args):
    payload = {"project": args.project, "title": args.title}
    target = getattr(args, "target", None)
    revision = getattr(args, "revision", None)
    attachments = getattr(args, "attachments", None)
    if args.role == "research":
        if not args.body or not args.body.strip() or not target or not revision:
            raise ValueError("research には body、target、revision が要る")
        if args.verify is not None or args.examples is not None or args.boundaries is not None:
            raise ValueError("research では verify と acceptance を使えない")
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
            raise ValueError("revision は小文字の完全な Git commit ID にする")
        if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", item) for item in [target, *(attachments or [])]):
            raise ValueError("target と attachment は登録済みの ID を指定する")
        payload.update(target=target, revision=revision)
        if attachments is not None:
            payload["attachments"] = attachments
    elif target is not None or revision is not None or attachments is not None:
        raise ValueError("target、revision、attachment は research 専用")
    for name in ("role", "body", "verify"):
        value = getattr(args, name, None)
        if value is not None:
            payload[name] = value
    examples = getattr(args, "examples", None)
    boundaries = getattr(args, "boundaries", None)
    if examples is not None or boundaries is not None:
        payload["acceptance"] = {}
        if examples is not None:
            payload["acceptance"]["examples"] = examples
        if boundaries is not None:
            payload["acceptance"]["boundaries"] = boundaries
    return payload
