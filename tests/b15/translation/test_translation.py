from __future__ import annotations

import json

import pytest

from plugins.subloop.release.translation import (
    ContributionPolicy,
    TranslationChange,
    TranslationService,
    TranslationSubmission,
)
from tests.fakes.b15.translation.parts import translation_json


HEAD = "b" * 40
POLICY = ContributionPolicy(
    repo="example-owner/dreamforge-rpg",
    allowed_path_pattern=r"translations/[a-z]{2,3}\.json",
)


def submission(*changes, head=HEAD, repo=POLICY.repo):
    return TranslationSubmission(repo=repo, head=head, changes=tuple(changes))


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (tuple(TranslationChange(f"translations/{name}.json", translation_json(a="b")) for name in ("ja", "ko", "zh", "fr")), "file_limit"),
        ((TranslationChange("translations/ja.json", b"x" * 2_000_001),), "file_too_large"),
        ((TranslationChange("README.md", translation_json(a="b")),), "path_forbidden"),
        ((TranslationChange("translations/ja.json", b"not-json"),), "invalid_translation_json"),
        ((TranslationChange("translations/ja.json", translation_json(a="b"), is_symlink=True),), "symlink_forbidden"),
    ],
)
def test_t36_rejects_invalid_external_translation(changes, reason):
    result = TranslationService().validate(submission(*changes), POLICY, HEAD)

    assert result.status == "rejected"
    assert result.reason == reason


def test_t36_requires_registered_type_and_verified_head():
    service = TranslationService()
    valid_change = TranslationChange("translations/ja.json", translation_json(hello="こんにちは"))

    unregistered = service.validate(submission(valid_change, repo="example-owner/hibi"), None, HEAD)
    moved = service.validate(submission(valid_change, head="c" * 40), POLICY, HEAD)

    assert unregistered.reason == "contribution_unregistered"
    assert moved.reason == "head_mismatch"


def test_t36_prepares_sync_fragment_and_keeps_destination_only_language():
    service = TranslationService()
    checked = service.validate(
        submission(TranslationChange("translations/ja.json", translation_json(hello="新しい翻訳"))), POLICY, HEAD
    )
    plan = service.prepare_sync(
        checked,
        current_head=HEAD,
        policy=POLICY,
        destination_languages={"ja": translation_json(old="旧"), "ko": translation_json(keep="保持")},
        open_sync_prs=1,
        branch_cleanup=False,
    )

    assert plan["status"] == "ready"
    assert json.loads(plan["sync_pr"]["files"]["ko"]) == {"keep": "保持"}
    assert plan["sync_pr"]["archived_branch"] is None
    assert plan["fragment"] == {
        "kind": "translation",
        "source_repo": POLICY.repo,
        "source_head": HEAD,
        "paths": ["src/SodRpg.Mod/lang/ja.json"],
    }


def test_t36_parks_sync_for_head_movement_or_multiple_prs():
    service = TranslationService()
    checked = service.validate(
        submission(TranslationChange("translations/ja.json", translation_json(hello="こんにちは"))), POLICY, HEAD
    )

    moved = service.prepare_sync(
        checked, current_head="c" * 40, policy=POLICY, destination_languages={}, open_sync_prs=0, branch_cleanup=True
    )
    multiple = service.prepare_sync(
        checked, current_head=HEAD, policy=POLICY, destination_languages={}, open_sync_prs=2, branch_cleanup=True
    )

    assert moved == {"status": "parked", "reason": "head_moved", "recheck_head": "c" * 40}
    assert multiple == {"status": "parked", "reason": "multiple_sync_prs"}
