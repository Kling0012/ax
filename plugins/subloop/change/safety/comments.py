"""Comment checkpoints and information-wait handling (S-19.3/S-19.4)."""
from __future__ import annotations

from copy import deepcopy
import json
from .input import prepare_input, timestamp


def comment_key(comment: dict) -> str:
    return json.dumps([comment["repo"].lower(), comment["item_number"], str(comment["comment_id"])], separators=(",", ":"))


def comment_position(comment: dict) -> dict:
    position = {key: comment[key] for key in ("repo", "item_number", "comment_id", "author", "created_at", "revision", "origin")}
    if any(not isinstance(position[k], str) or not position[k] for k in ("repo", "author", "created_at", "revision", "origin")):
        raise ValueError("complete comment metadata required")
    if type(position["item_number"]) is not int or position["item_number"] < 1:
        raise ValueError("positive item number required")
    if type(position["comment_id"]) not in {str, int} or not str(position["comment_id"]):
        raise ValueError("comment ID required")
    return position


def process_comments(comments: list[dict], *, gate, cutover_time, cutover_visible: bool,
                     positions: dict | None = None, posted_ids=(), sensitivity: str,
                     proposer: str, waiting_information=False, completed=False,
                     max_bytes=None, admit) -> dict:
    """Call at every stage boundary and again immediately before merge.

    positions maps composite comment keys to the complete checked record. Pass
    the returned mapping back on the next call; do not substitute a date cursor.
    admit(comment) atomically reserves a comment quota through AdmissionGate.
    """
    if not callable(admit):
        raise ValueError("atomic comment admission callback required")
    positions = deepcopy(positions or {})
    output = {"positions": positions, "safe_comments": [], "context": [], "ignored": [],
              "pending": [], "resume": False, "new_work": False,
              "waiting_information": waiting_information, "merge_allowed": True}
    cutoff = timestamp(cutover_time) if cutover_visible and cutover_time is not None else None
    posted = {(repo, number, str(cid)) for repo, number, cid in posted_ids}
    for comment in comments:
        try:
            position = comment_position(comment)
            key = comment_key(comment)
            created = timestamp(comment["created_at"])
            prepared = prepare_input(comment["body"], max_bytes=max_bytes)
        except (KeyError, ValueError, TypeError):
            output["pending"].append({"reason": "invalid_comment"})
            output["merge_allowed"] = False
            continue
        if positions.get(key) == position:
            continue
        if cutoff is None or created <= cutoff:
            # Do not advance a context-only checkpoint: a later cutover event
            # may establish that this comment was created after cutover.
            output["context"].append({"position": position, "text": prepared.body, "state": prepared.state})
            continue
        if comment["origin"] != "human" or (comment["repo"], comment["item_number"], str(comment["comment_id"])) in posted:
            output["ignored"].append(position)
            positions[key] = position
            continue
        output["merge_allowed"] = False
        if admit is not None:
            try:
                admission = admit(deepcopy(comment))
            except Exception:
                admission = {"state": "queued", "reason": "admission_unavailable"}
            if admission.get("state") != "ready":
                output["pending"].append({"position": position, "reason": admission.get("reason", "rate_limit")})
                continue
        review = gate.review(prepared, sensitivity=sensitivity, kind="comment")
        if review["state"] != "ready":
            output["ignored"].append({"position": position, "reason": review["reason"]})
            # Mark rejected versions checked; they can never become requirements.
            positions[key] = position
            continue
        output["safe_comments"].append({"position": position, "worker_input": review["worker_input"],
                                        "input_removed": review["input_removed"]})
        positions[key] = position
        if waiting_information and comment["author"] == proposer:
            output["resume"] = True
            output["waiting_information"] = False
        if completed:
            output["new_work"] = True
    return output


def issue_route(kind: str, *, information_required=False, cause_known=False, research_available=True) -> dict:
    if information_required:
        return {"route": "ask_proposer", "state": "waiting_information"}
    if kind == "question":
        return {"route": "research_answer" if research_available else "answer_unknown", "state": "ready"}
    if kind == "bug":
        return {"route": "tdd-change" if cause_known else "diagnose", "state": "ready"}
    # Ambiguity alone is not a reason to park an issue.
    return {"route": "tdd-change", "state": "ready"}


def information_deadline(*, deadline, now, safe_answer=False) -> dict:
    if safe_answer:
        return {"state": "ready", "reason": "safe_answer"}
    if timestamp(now) >= timestamp(deadline):
        return {"state": "parked", "reason": "information_deadline", "accept_later_answer": True}
    return {"state": "waiting_information", "check_at": timestamp(deadline).isoformat()}
