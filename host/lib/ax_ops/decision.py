"""CLI argument and request helpers for decision.record."""
import hashlib
import json


def add_parsers(commands):
    decide = commands.add_parser("decide")
    decide.add_argument("kind", choices=("decision", "preference", "handoff"))
    decide.add_argument("--title", required=True)
    decide.add_argument("--body", required=True)
    decide.add_argument("--project")
    decide.add_argument("--reason")
    decide.add_argument("--basis", choices=("user_said", "commander", "design"), default="commander")
    decide.add_argument("--quote")
    decide.add_argument("--supersedes")
    handoff = commands.add_parser("handoff")
    handoff.add_argument("body")
    handoff.add_argument("--project")


def build_request(args, global_args):
    if args.command == "handoff":
        payload = {"kind": "handoff", "title": "Handoff", "body": args.body, "basis": "commander"}
    else:
        payload = {key: getattr(args, key) for key in ("kind", "title", "body", "basis", "reason", "quote", "supersedes") if getattr(args, key, None) is not None}
    if args.project is not None:
        payload["project"] = args.project
    if payload["basis"] == "user_said" and not (payload.get("quote") or "").strip():
        raise ValueError("quote is required for user_said")
    canonical = json.dumps({"destination": "decision.record", "payload": payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    request_id = global_args.request_id or hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not 1 <= len(request_id) <= 200:
        raise ValueError("request-id must be 1–200 characters")
    body = {"from": "commander", "destination": "decision.record", "payload": payload, "idempotency_key": request_id}
    if args.project:
        body["project"] = args.project
    if global_args.session:
        body["session"] = global_args.session
    return "POST", "/v1/instructions", body, request_id
