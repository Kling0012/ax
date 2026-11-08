"""外部翻訳の検査と非公開側への同期計画（S-16.6、S-20.8）。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ContributionPolicy:
    """製品登録簿が repo ごとに渡す外部からの貢献の型。"""

    repo: str
    allowed_path_pattern: str
    max_files: int = 3
    max_bytes: int = 2_000_000
    sync_path_template: str = "src/SodRpg.Mod/lang/{language}.json"


@dataclass(frozen=True)
class TranslationChange:
    path: str
    content: bytes
    is_symlink: bool = False


@dataclass(frozen=True)
class TranslationSubmission:
    repo: str
    head: str
    changes: tuple[TranslationChange, ...]


@dataclass(frozen=True)
class TranslationValidation:
    status: str
    reason: str
    repo: str
    checked_head: str | None
    changes: tuple[TranslationChange, ...] = ()


class TranslationService:
    """公開翻訳を検査し、外部操作をせず同期 PR の入力だけを作る。"""

    def validate(
        self,
        submission: TranslationSubmission,
        policy: ContributionPolicy | None,
        expected_head: str,
    ) -> TranslationValidation:
        if policy is None or policy.repo != submission.repo:
            return TranslationValidation("rejected", "contribution_unregistered", submission.repo, None)
        if submission.head != expected_head:
            return TranslationValidation("rejected", "head_mismatch", submission.repo, expected_head)
        if not submission.changes or len(submission.changes) > policy.max_files:
            return TranslationValidation("rejected", "file_limit", submission.repo, expected_head)
        for change in submission.changes:
            if change.is_symlink:
                return TranslationValidation("rejected", "symlink_forbidden", submission.repo, expected_head)
            if not re.fullmatch(policy.allowed_path_pattern, change.path):
                return TranslationValidation("rejected", "path_forbidden", submission.repo, expected_head)
            if len(change.content) > policy.max_bytes:
                return TranslationValidation("rejected", "file_too_large", submission.repo, expected_head)
            if not self._valid_translation_json(change.content):
                return TranslationValidation("rejected", "invalid_translation_json", submission.repo, expected_head)
        return TranslationValidation("accepted", "ok", submission.repo, expected_head, submission.changes)

    def prepare_sync(
        self,
        validation: TranslationValidation,
        *,
        current_head: str,
        policy: ContributionPolicy,
        destination_languages: Mapping[str, bytes],
        open_sync_prs: int,
        branch_cleanup: bool,
    ) -> dict[str, Any]:
        """同期 PR と fragment の内容を作る。push、PR 作成、削除はしない。"""

        if validation.status != "accepted":
            return {"status": "rejected", "reason": validation.reason}
        if validation.checked_head != current_head:
            return {"status": "parked", "reason": "head_moved", "recheck_head": current_head}
        if open_sync_prs > 1:
            return {"status": "parked", "reason": "multiple_sync_prs"}

        files = dict(destination_languages)
        synchronized_paths: list[str] = []
        for change in validation.changes:
            language = change.path.rsplit("/", 1)[-1].removesuffix(".json")
            files[language] = change.content
            synchronized_paths.append(policy.sync_path_template.format(language=language))
        fragment = {
            "kind": "translation",
            "source_repo": validation.repo,
            "source_head": validation.checked_head,
            "paths": synchronized_paths,
        }
        return {
            "status": "ready",
            "sync_pr": {
                "source_head": validation.checked_head,
                "files": files,
                "paths": synchronized_paths,
                "archived_branch": None if not branch_cleanup else "eligible_after_merge",
            },
            "fragment": fragment,
        }

    @staticmethod
    def _valid_translation_json(content: bytes) -> bool:
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(value, dict) and all(isinstance(key, str) and isinstance(item, str) for key, item in value.items())
