"""In-process Chat Completions scripts; no HTTP server or model client."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import json


class ModelFailure(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        self.body = {"error": {"code": "rate_limited" if status == 429 else "unavailable"}}
        super().__init__(f"scripted model: {status}")


class ScriptedModel:
    def __init__(self, case: dict, mode: str = "good", *, faults=()):
        if mode not in {"good", "injectable", "sloppy"}:
            raise ValueError("mode must be good, injectable or sloppy")
        self.case = deepcopy(case)
        self.mode = mode
        self._initial_faults = tuple(faults)
        self.reset()

    def reset(self):
        key = "bad" if self.mode == "injectable" and self.case["id"].startswith("I-") else "golden"
        self.steps = deepcopy(self.case[key])
        if self.mode == "sloppy":
            first = next((step for step in self.steps if "tool" in step), None)
            if first is None or not first.get("args"):
                raise ValueError("sloppy needs a first call with an argument")
            # Change exactly one argument; do not mutate the fixture or later turns.
            # Corrupt an argument actually checked by required, when present.
            # Merely damaging request_id could leave a required title/project
            # matcher satisfied even though the fake intake refuses the call.
            matched = next((rule for rule in self.case.get("required", [])
                            if rule.get("tool") == first["tool"]), {})
            checked = [*matched.get("args", {}), *matched.get("args_contains", {})]
            arg = next((key.split(".")[0] for key in checked
                        if key.split(".")[0] in first["args"]), next(iter(first["args"])))
            first["args"][arg] = {"malformed": True}
        self._cursor = 0
        self._faults = deque(self._initial_faults)
        self.calls = []
        self.final_response = ""

    def respond(self, messages=None, tools=None) -> dict:
        self.calls.append(deepcopy({"messages": messages or [], "tools": tools or []}))
        if self._faults:
            status = self._faults.popleft()
            if status not in (429, 503):
                raise ValueError("only fake 429 and 503 failures are supported")
            raise ModelFailure(status)
        if self._cursor >= len(self.steps):
            raise RuntimeError("script exhausted; reset before another run")
        step = self.steps[self._cursor]
        self._cursor += 1
        if "tool" in step:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call-{self._cursor}", "type": "function", "function": {
                    "name": step["tool"], "arguments": json.dumps(step["args"], ensure_ascii=False)}}]}
            reason = "tool_calls"
        else:
            self.final_response = step["final"]
            message = {"role": "assistant", "content": self.final_response}
            reason = "stop"
        return {"id": f"script-{self._cursor}", "object": "chat.completion", "model": "exam-model",
                "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    def chat_completions(self, payload: dict) -> dict:
        return self.respond(payload.get("messages"), payload.get("tools"))

    def handle(self, method: str, path: str, body=None) -> tuple[int, dict]:
        if path != "/v1/chat/completions":
            return 404, {"error": {"code": "not_found"}}
        if method != "POST":
            return 405, {"error": {"code": "method_not_allowed"}}
        try:
            return 200, self.chat_completions(body or {})
        except ModelFailure as exc:
            return exc.status, exc.body
