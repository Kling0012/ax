from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from plugins.subloop.change.safety import (
    AdmissionGate, SafetyGate, classification_key, close_plan, preclassify, prepare_input, sanitize,
)
from tests.fakes.b14.safety.ports import InjectionCheck, Model, Store, safe

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)


def test_t22_hidden_html_and_invisible_removed_before_model():
    raw = 'visible<!--secret--><span hidden>secret</span><i style="display: none">secret</i><script>secret</script>​&#8203;tail'
    prepared = prepare_input(raw)
    assert prepared.body == "visibletail"
    assert prepared.removed["body"]
    models = [Model(safe()), Model(safe())]
    gate = SafetyGate([[models[0].candidate("a")], [models[1].candidate("b")]], injection_check=InjectionCheck())
    assert gate.review(prepared, sensitivity="public")["state"] == "ready"
    assert all(m.requests[0]["input"]["body"] == "visibletail" for m in models)


@pytest.mark.parametrize("raw", ["<div hidden><span>x</span></div>ok", "<template>x</template>ok", "<span aria-hidden='true'>x</span>ok"])
def test_t22_hidden_nested(raw):
    assert sanitize(raw)[0] == "ok"


@pytest.mark.parametrize("field", ["body", "diff"])
def test_t22_oversize_is_parked_without_truncation(field):
    data = {"body": "ok", "diff": ""}
    data[field] = "あ" * 3
    prepared = prepare_input(**data, max_bytes=8)
    model = Model(safe())
    result = preclassify(prepared, [model.candidate()], sensitivity="public")
    assert result["action"] == "parked" and not model.requests
    assert prepared.body is None and prepared.diff is None
    assert prepare_input("あ" * 2, max_bytes=6).state == "ready"
    assert prepare_input("<!--" + "x" * 10 + "-->", max_bytes=8).state == "parked"


@pytest.mark.parametrize("outcomes,expected", [
    (["spam", "spam"], "close_candidate"), (["unrelated", "unrelated"], "close_candidate"),
    (["spam", "unknown"], "safety_review"), (["spam", "unrelated"], "safety_review"),
    (["unknown", "unknown"], "safety_review"), (["relevant", "relevant"], "safety_review"),
])
def test_t22_two_consecutive_complete_classifications(outcomes, expected):
    model = Model(*[{"classification": c} for c in outcomes])
    result = preclassify(prepare_input("x"), [model.candidate()], sensitivity="public")
    assert result["action"] == expected
    assert model.requests[0] == model.requests[1]


def test_t22_partial_classification_fallback_and_sensitive_eligibility():
    free = Model({"classification": "spam"})
    incomplete = Model({}, {})
    qualified = Model({"classification": "spam"}, {"classification": "unknown"})
    result = preclassify(prepare_input("private context"),
        [free.candidate("free", "free"), incomplete.candidate("partial"), qualified.candidate("qualified")], sensitivity="sensitive")
    assert result["action"] == "safety_review"
    assert not free.requests and len(qualified.requests) == 2


def test_t22_atomic_author_item_and_comment_budgets_and_rolling_day():
    store = Store()
    gate = AdmissionGate(store)
    def admit(i):
        return gate.reserve(key=f"item:{i}", author="author", kind="issue" if i % 2 else "pr", now=NOW)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(admit, range(8)))
    assert sum(r["state"] == "ready" for r in results) == 3
    queued = next(r for r in results if r["state"] == "queued")
    assert queued["check_at"] == (NOW + timedelta(hours=24)).isoformat()
    assert gate.reserve(key=store.records[0]["key"], author="author", kind="issue", now=NOW)["reserved"] is False
    comments = [gate.reserve(key=f"comment:{i}", author="author", kind="comment", now=NOW) for i in range(11)]
    assert sum(r["state"] == "ready" for r in comments) == 10
    assert gate.reserve(key="later", author="author", kind="issue", now=NOW + timedelta(hours=24))["state"] == "ready"


def test_t22_global_budget_separate_from_comment_budget():
    gate = AdmissionGate(Store())
    for i in range(10):
        assert gate.reserve(key=str(i), author=str(i), kind="issue", now=NOW)["state"] == "ready"
    assert gate.reserve(key="overflow", author="new", kind="pr", now=NOW)["reason"] == "global_limit"
    assert gate.reserve(key="comment", author="new", kind="comment", now=NOW)["state"] == "ready"


def test_t22_classification_binding_changes_for_all_versions():
    c = [{"comment_id": 1, "revision": "r1"}]
    key = classification_key("body-v1", c, "p1")
    assert key == classification_key("body-v1", list(reversed(c)), "p1")
    assert key != classification_key("body-v2", c, "p1")
    assert key != classification_key("body-v1", [{"comment_id": 1, "revision": "r2"}], "p1")
    assert key != classification_key("body-v1", c, "p2")


def test_t22_close_requires_permission_outbound_check_and_dry_run():
    item = {"issue_repo": "owner/repo", "number": 3}
    result = {"action": "close_candidate", "outcomes": ["spam", "spam"]}
    assert close_plan(item, result, permission_check=lambda p: False, outbound_check=lambda p: True)["state"] == "waiting_user"
    assert close_plan(item, result, permission_check=lambda p: True, outbound_check=lambda p: False)["state"] == "parked"
    plan = close_plan(item, result, permission_check=lambda p: True, outbound_check=lambda p: True)
    assert plan["state"] == "planned" and plan["execute"] is False
    result["outcomes"] = ["spam", None]
    assert close_plan(item, result, permission_check=lambda p: True, outbound_check=lambda p: True)["state"] == "parked"


def test_t22_malformed_attributes_and_non_utf8_remain_item_local():
    assert prepare_input("<span style aria-hidden>visible</span>").body == "visible"
    assert prepare_input("\ud800").state == "parked"


def test_t22_complete_realenv_classification_reused_only_for_exact_binding():
    from plugins.subloop.change.safety.input import ClassificationMemo
    from axcommon.schemas import load
    from jsonschema import Draft202012Validator
    complete = Draft202012Validator(load("ext/b14/safety/review")["$defs"]["classification"]).is_valid
    records, calls = {}, []
    memo = ClassificationMemo(records)
    def classify():
        calls.append(True)
        return {"classification": "unknown"}
    params = {"body_revision": "v1", "comments": [{"comment_id": 1, "revision": "v1"}], "policy_revision": "p1", "classify": classify, "complete": complete}
    assert memo.resolve(**params) == {"classification": "unknown"}
    assert ClassificationMemo(records).resolve(**params) == {"classification": "unknown"}
    assert len(calls) == 1
    for overrides in ({"body_revision": "v2"}, {"comments": [{"comment_id": 1, "revision": "v2"}]}, {"policy_revision": "p2"}):
        assert memo.resolve(**{**params, **overrides}) is not None
    assert len(calls) == 4
    assert memo.resolve(**{**params, "body_revision": "bad", "classify": lambda: {}}) is None
    assert len(records) == 4
