"""S-25.3 requests for B14b2. This module never reads credentials."""
from __future__ import annotations

import re
from typing import Mapping

from .contracts import Draft, EnvironmentRunner, EnvResult, Target


class XUnavailable(RuntimeError):
    """Only X-dependent work is parked."""


class XEnvironment:
    def __init__(self, runner: EnvironmentRunner):
        self.runner = runner

    @staticmethod
    def _request(job_id: str, name: str, args: dict, outputs: list[str],
                 irreversible: bool) -> dict:
        if not re.fullmatch(r"[a-z0-9-]{1,60}", job_id):
            raise ValueError("invalid env job ID")
        return {
            "job_id": job_id, "requires": ["x-session"], "sensitivity": "public",
            "entry": {"kind": "entrypoint", "name": name, "args": args},
            "outputs": outputs, "secrets": [],
            "budget_min": 10 if irreversible else 15, "stall_min": 5,
            "irreversible": irreversible, "scope": "one", "idempotent": False,
            "idempotency_key": job_id,
        }

    def collect(self, job_id: str, *, query: str | None = None, limit: int = 40) -> EnvResult:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid collection limit")
        if query is not None and not re.fullmatch(r"#[^\s#]{1,40}", query):
            raise ValueError("invalid search query")
        args = {"op": "stats" if query is None else "search", "limit": limit}
        if query is not None:
            args["query"] = query
        output = "stats.json" if query is None else "search.jsonl"
        result = self.runner.run(self._request(job_id, "x-collect", args, [output], False), {})
        if result.state != "succeeded":
            raise XUnavailable("x_collection_unavailable")
        return result

    @staticmethod
    def artifacts(draft: Draft) -> dict[str, bytes]:
        names = draft.images
        if names != () and names != ("1.mp4",):
            if not names or len(names) > 4 or tuple(sorted(set(names))) != names:
                raise ValueError("invalid images")
            if any(name not in ("1.png", "2.png", "3.png", "4.png") for name in names):
                raise ValueError("invalid images")
        if set(draft.artifacts) != set(names):
            raise ValueError("unexpected or missing attachment")
        if not isinstance(draft.text, str) or not draft.text.strip():
            raise ValueError("empty text")
        if any(not isinstance(value, bytes) or not value for value in draft.artifacts.values()):
            raise ValueError("invalid attachment content")
        return {"text.txt": draft.text.encode("utf-8"), **draft.artifacts}

    def post(self, job_id: str, draft: Draft) -> EnvResult:
        artifacts = self.artifacts(draft)
        request = self._request(job_id, "x-post", {"text": "text.txt", "images": list(draft.images)},
                                ["result.json"], True)
        return self.runner.run(request, artifacts)

    @staticmethod
    def receipt(target: Target, output: Mapping) -> str | None:
        """Only the result artifact, never stdout/logs, can confirm publication."""
        value = output.get("url")
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", target.account):
            return None
        pattern = r"https://x\.com/" + re.escape(target.account) + r"/status/([0-9]+)"
        match = re.fullmatch(pattern, value)
        return match.group(1) if match else None
