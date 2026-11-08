"""実行環境で共有する値オブジェクト（S-25.2〜S-25.4）。

この層は接続、資格、実行器を持たない。依頼は能力と区分だけを渡し、
環境 ID は台帳と選択器の内部でだけ使う。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from axcommon.policy import SENSITIVITIES, most_restrictive

ADAPTERS = frozenset({"local", "ssh-host", "container", "cloud-sandbox"})
ENVIRONMENT_STATES = frozenset({"trial", "available", "degraded", "excluded", "draining", "retired"})
SCOPES = frozenset({"one", "each"})


class EnvironmentContractError(ValueError):
    """環境台帳または環境ジョブが共通契約に合わない。"""


def _names(values: Any, *, field: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise EnvironmentContractError(f"{field} は一覧にする")
    result = tuple(values)
    if not result and not allow_empty:
        raise EnvironmentContractError(f"{field} を空にできない")
    if any(not isinstance(value, str) or not value for value in result):
        raise EnvironmentContractError(f"{field} に空でない文字列だけを入れる")
    return tuple(dict.fromkeys(result))


def effective_sensitivity(declared: str, *observed: str | None) -> str:
    """依頼、入力、出力から実効区分を決める。区分を下げない。"""
    values = [declared, *(value for value in observed if value is not None)]
    if any(value not in SENSITIVITIES for value in values):
        raise EnvironmentContractError("sensitivity が不正")
    return most_restrictive(*values)


@dataclass(frozen=True)
class EnvironmentJob:
    """S-25.3 の環境ジョブ。資格はスロット名だけを持つ。"""

    job_id: str
    requires: tuple[str, ...]
    sensitivity: str
    entry: Mapping[str, Any]
    outputs: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    scope: str = "one"
    user_requested: bool = False
    irreversible: bool = False
    idempotent: bool = False
    idempotency_key: str | None = None
    trial_for: str | None = None
    resident_output: bool = False
    input_sensitivities: tuple[str, ...] = ()
    output_sensitivity: str | None = None

    def __post_init__(self) -> None:
        _names(self.requires, field="requires")
        _names(self.outputs, field="outputs", allow_empty=True)
        _names(self.secrets, field="secrets", allow_empty=True)
        if self.scope not in SCOPES:
            raise EnvironmentContractError("scope が不正")
        if not isinstance(self.entry, Mapping) or not isinstance(self.entry.get("kind"), str):
            raise EnvironmentContractError("entry.kind が不正")
        effective_sensitivity(self.sensitivity, *self.input_sensitivities, self.output_sensitivity)
        if self.trial_for is not None and (not self.trial_for or self.effective_sensitivity != "public" or self.secrets):
            raise EnvironmentContractError("trial のジョブは public で secrets を持たない")

    @property
    def effective_sensitivity(self) -> str:
        return effective_sensitivity(self.sensitivity, *self.input_sensitivities, self.output_sensitivity)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EnvironmentJob":
        try:
            job_id = data["job_id"]
            sensitivity = data["sensitivity"]
            entry = data["entry"]
        except KeyError as error:
            raise EnvironmentContractError(f"必須項目が無い: {error.args[0]}") from None
        if not isinstance(job_id, str) or not job_id:
            raise EnvironmentContractError("job_id が不正")
        if not isinstance(entry, Mapping) or not isinstance(entry.get("kind"), str):
            raise EnvironmentContractError("entry.kind が不正")
        scope = data.get("scope", "one")
        if scope not in SCOPES:
            raise EnvironmentContractError("scope が不正")
        trial_for = data.get("trial_for")
        if trial_for is not None and (not isinstance(trial_for, str) or not trial_for):
            raise EnvironmentContractError("trial_for が不正")
        secrets = _names(data.get("secrets", ()), field="secrets", allow_empty=True)
        if trial_for is not None and (sensitivity != "public" or secrets):
            raise EnvironmentContractError("trial のジョブは public で secrets を持たない")
        input_sensitivities = _names(data.get("input_sensitivities", ()), field="input_sensitivities", allow_empty=True)
        output_sensitivity = data.get("output_sensitivity")
        if output_sensitivity is not None and not isinstance(output_sensitivity, str):
            raise EnvironmentContractError("output_sensitivity が不正")
        return cls(
            job_id=job_id,
            requires=_names(data.get("requires", ()), field="requires"),
            sensitivity=sensitivity,
            entry=dict(entry),
            outputs=_names(data.get("outputs", ()), field="outputs", allow_empty=True),
            secrets=secrets,
            scope=scope,
            user_requested=bool(data.get("user_requested", False)),
            irreversible=bool(data.get("irreversible", False)),
            idempotent=bool(data.get("idempotent", False)),
            idempotency_key=data.get("idempotency_key"),
            trial_for=trial_for,
            resident_output=bool(data.get("resident_output", False)),
            input_sensitivities=input_sensitivities,
            output_sensitivity=output_sensitivity,
        )


@dataclass(frozen=True)
class SelectionRequest:
    """副作用のない選択器への入力。"""

    requires: tuple[str, ...]
    sensitivity: str
    user_requested: bool = False
    scope: str = "one"
    trial_for: str | None = None
    resident_output: bool = False
    input_sensitivities: tuple[str, ...] = ()
    output_sensitivity: str | None = None

    def __post_init__(self) -> None:
        _names(self.requires, field="requires")
        if self.scope not in SCOPES:
            raise EnvironmentContractError("scope が不正")
        effective_sensitivity(self.sensitivity, *self.input_sensitivities, self.output_sensitivity)
        if self.trial_for is not None and (not self.trial_for or self.effective_sensitivity != "public"):
            raise EnvironmentContractError("trial の選択は public に限る")

    @property
    def effective_sensitivity(self) -> str:
        return effective_sensitivity(self.sensitivity, *self.input_sensitivities, self.output_sensitivity)

    @classmethod
    def from_job(cls, job: EnvironmentJob) -> "SelectionRequest":
        return cls(
            requires=job.requires,
            sensitivity=job.sensitivity,
            user_requested=job.user_requested,
            scope=job.scope,
            trial_for=job.trial_for,
            resident_output=job.resident_output,
            input_sensitivities=job.input_sensitivities,
            output_sensitivity=job.output_sensitivity,
        )
