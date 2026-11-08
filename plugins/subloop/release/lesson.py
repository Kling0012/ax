"""Lesson selection using injected timeline access (S-20.9)."""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable, Mapping, Any


class LessonService:
    """Record observations and explicitly promote evidenced lessons.

    ``events`` returns persisted timeline events for a kind; ``sink`` implements
    EventSink.emit. Selection and retrieval failures propagate so a caller cannot
    start a worker with an incomplete set of required lessons.
    """

    def __init__(self, sink: Any, events: Callable[[str], Iterable[Mapping[str, Any]]]):
        self.sink = sink
        self.events = events

    def record_candidate(self, *, target: str, what: str, cause: str, source_ref: str) -> bool:
        if not all(isinstance(x, str) and x.strip() for x in (target, what, cause, source_ref)):
            return False
        try:
            self.sink.emit("lesson.candidate", target, {
                "target": target, "what": what, "cause": cause, "source_ref": source_ref,
            })
            return True
        except Exception:
            # Observation is best effort and must never block the original job.
            return False

    def select(self, *, lesson_id: str, target: str, what: str, cause: str,
               body: str, serious: bool = False) -> Mapping[str, Any]:
        if not all(isinstance(x, str) and x.strip() for x in (lesson_id, target, what, cause, body)):
            raise ValueError("lesson fields must be nonempty")
        if not isinstance(serious, bool):
            raise ValueError("serious must be boolean")
        if any(e["data"]["lesson_id"] == lesson_id for e in self.events("lesson.selected")):
            raise ValueError("lesson_id already selected")
        sources = sorted({d["source_ref"] for e in self.events("lesson.candidate")
                          if (d := e["data"]).get("target") == target
                          and d.get("what") == what and d.get("cause") == cause
                          and isinstance(d.get("source_ref"), str) and d["source_ref"].strip()})
        if not sources or (not serious and len(sources) < 2):
            raise ValueError("lesson requires serious or repeated evidence")
        data = {"lesson_id": lesson_id, "target": target, "what": what, "cause": cause,
                "body": body, "source_refs": sources, "reason": "serious" if serious else "repeated"}
        return self.sink.emit("lesson.selected", target, data)

    def for_worker(self, *, target: str, parts: Any) -> Any:
        """Return worker PromptParts with selected lessons for this exact target."""
        if not isinstance(target, str) or not target.strip():
            raise ValueError("target is required")
        lessons = []
        for event in self.events("lesson.selected"):
            data = event["data"]
            if not isinstance(data.get("target"), str) or not data["target"].strip():
                raise ValueError("invalid selected lesson target")
            if data["target"] == target:
                if not all(isinstance(data.get(key), str) and data[key].strip() for key in ("lesson_id", "body")):
                    raise ValueError("invalid selected lesson")
                lessons.append((data["lesson_id"], data["body"]))
        if len({lid for lid, _ in lessons}) != len(lessons):
            raise ValueError("duplicate selected lesson id")
        return replace(parts, lessons=tuple(sorted(lessons)))


def timeline_events(client: Any, kind: str) -> Iterable[Mapping[str, Any]]:
    """Read every persisted event, rejecting incomplete or malformed pages."""
    after = 0
    while True:
        page = client.get_events(after=after, kind=kind, limit=1000, wait=0)
        if not isinstance(page, dict) or not isinstance(page.get("events"), list):
            raise ValueError("invalid timeline page")
        rows = page["events"]
        cursor = page.get("next_after")
        if type(cursor) is not int or (rows and cursor <= after) or (not rows and cursor != after):
            raise ValueError("invalid timeline cursor")
        for row in rows:
            if not isinstance(row, dict) or row.get("kind") != kind or not isinstance(row.get("data"), dict):
                raise ValueError("invalid timeline event")
            yield row
        if len(rows) < 1000:
            return
        after = cursor
