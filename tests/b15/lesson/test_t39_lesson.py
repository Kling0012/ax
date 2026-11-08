import importlib

import pytest

from plugins.subloop.release.lesson import LessonService

prompt = importlib.import_module("plugins.resource.worker-pool.prompt")


class Timeline:
    def __init__(self):
        self.rows = []
        self.fail = False

    def emit(self, kind, subject, data):
        if self.fail:
            raise OSError("timeline unavailable")
        event = {"kind": kind, "subject": subject, "data": data}
        self.rows.append(event)
        return event

    def events(self, kind):
        if self.fail:
            raise OSError("timeline unavailable")
        return [row for row in self.rows if row["kind"] == kind]


def test_candidate_selection_to_worker_prompt():
    timeline = Timeline()
    lessons = LessonService(timeline, timeline.events)
    candidate = dict(target="project-a", what="test failure", cause="missing stub")
    assert lessons.record_candidate(**candidate, source_ref="run-1")
    assert lessons.record_candidate(**candidate, source_ref="run-1")
    with pytest.raises(ValueError):
        lessons.select(**candidate, lesson_id="l2", body="Update the stub")
    assert lessons.record_candidate(**candidate, source_ref="run-2")
    lessons.select(**candidate, lesson_id="l2", body="Update the stub")
    assert timeline.rows[-1]["data"]["source_refs"] == ["run-1", "run-2"]
    assert lessons.record_candidate(target="project-a", what="outage", cause="bad config", source_ref="run-3")
    lessons.select(target="project-a", what="outage", cause="bad config", lesson_id="l1", body="Check config", serious=True)
    assert lessons.record_candidate(target="project-a", what="unselected", cause="unknown", source_ref="run-4")
    base = prompt.PromptParts(standing="rules", project="project rules", job="do work")
    text = prompt.build(lessons.for_worker(target="project-a", parts=base))
    assert text.index("l1\nCheck config") < text.index("l2\nUpdate the stub") < text.index("do work")
    assert "unselected" not in text
    assert "Update the stub" not in prompt.build(lessons.for_worker(target="project-b", parts=base))


def test_candidate_failure_is_soft_but_required_lesson_failure_is_not():
    timeline = Timeline()
    lessons = LessonService(timeline, timeline.events)
    timeline.fail = True
    assert not lessons.record_candidate(target="p", what="w", cause="c", source_ref="r")
    with pytest.raises(OSError):
        lessons.for_worker(target="p", parts=prompt.PromptParts())
    with pytest.raises(OSError):
        lessons.select(target="p", what="w", cause="c", lesson_id="l", body="b", serious=True)
