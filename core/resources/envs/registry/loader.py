"""`config/environments.yaml` の読み込みと局所的な拒否（S-25.2、D-23）。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from axcommon import schemas

from ..contracts import ADAPTERS, ENVIRONMENT_STATES

_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_CAPABILITY = re.compile(r"[a-z0-9][a-z0-9:-]*\Z")
_REQUIRED = frozenset({
    "id", "adapter", "connect", "conn_credential", "os", "capabilities", "entrypoints",
    "data_classes", "secrets_allowed", "work_credentials", "secret_targets", "max_concurrent",
    "cost", "lifetime", "owner_use", "rank", "registered_at", "state",
})
_CREDENTIAL_KEYS = frozenset({"slot", "name", "location", "place", "var", "variable", "vars", "header"})
_SECRET_TARGET_KEYS = frozenset({"name", "path", "mode", "max_bytes", "slot"})
_VALUE_KEYS = frozenset({"value", "secret", "token", "password", "passwd", "api_key", "private_key"})


class RegistryError(ValueError):
    """台帳全体の形式が読めない。個別環境の不備は rejected に入れる。"""


@dataclass(frozen=True)
class EnvironmentRegistry:
    """台帳の見出し、読めた環境、環境ごとの拒否理由。"""

    contract_supported: tuple[str, ...]
    environments: tuple[dict[str, Any], ...]
    rejected: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def get(self, environment_id: str) -> dict[str, Any] | None:
        return next((entry for entry in self.environments if entry["id"] == environment_id), None)


def default_environment_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    root = Path(__file__).resolve().parents[4]
    if env.get("AX_ENVIRONMENTS_FILE"):
        return root / env["AX_ENVIRONMENTS_FILE"]
    configured = root / "config" / "environments.yaml"
    return configured if configured.is_file() else root / "config" / "environments.example.yaml"


def _contains_secret_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(str(key).lower() in _VALUE_KEYS or _contains_secret_value(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret_value(item) for item in value)
    return False


def _credential_errors(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"{field} はオブジェクトにする"]
    keys = set(value)
    errors = []
    if not (value.get("slot") or value.get("name")):
        errors.append(f"{field} に資格名が無い")
    for key in keys & (_CREDENTIAL_KEYS - {"vars"}):
        if not isinstance(value[key], str) or not value[key]:
            errors.append(f"{field}.{key} が不正")
    if "vars" in value and (not isinstance(value["vars"], list) or any(
        not isinstance(item, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item)
        for item in value["vars"]
    )):
        errors.append(f"{field}.vars が不正")
    if not keys <= _CREDENTIAL_KEYS:
        errors.append(f"{field} に資格の値または未定義の項目がある")
    if _contains_secret_value(value):
        errors.append(f"{field} に秘密の値を書けない")
    return errors


def _entry_errors(entry: Mapping[str, Any]) -> list[str]:
    errors = list(schemas.errors("environment", dict(entry)))
    missing = sorted(_REQUIRED - set(entry))
    if missing:
        errors.append("必須項目が無い: " + ", ".join(missing))
        return errors
    if entry.get("adapter") not in ADAPTERS:
        errors.append("adapter が不正")
    if entry.get("state") not in ENVIRONMENT_STATES:
        errors.append("state が不正")
    if not isinstance(entry.get("id"), str) or not _ID.fullmatch(entry["id"]):
        errors.append("id が不正")
    if not isinstance(entry.get("capabilities"), list) or any(
        not isinstance(capability, str) or not _CAPABILITY.fullmatch(capability)
        for capability in entry["capabilities"]
    ):
        errors.append("capabilities が不正")
    if not isinstance(entry.get("connect"), Mapping) or not entry["connect"].get("kind"):
        errors.append("connect.kind が無い")
    errors.extend(_credential_errors(entry.get("conn_credential"), field="conn_credential"))
    if "gate_credential" in entry:
        errors.extend(_credential_errors(entry["gate_credential"], field="gate_credential"))
    credentials = entry.get("work_credentials")
    if not isinstance(credentials, list):
        errors.append("work_credentials は一覧にする")
    else:
        for index, credential in enumerate(credentials):
            errors.extend(_credential_errors(credential, field=f"work_credentials[{index}]"))
    targets = entry.get("secret_targets")
    if not isinstance(targets, list):
        errors.append("secret_targets は一覧にする")
    else:
        for index, target in enumerate(targets):
            if not isinstance(target, Mapping) or not {"name", "path", "mode", "max_bytes"} <= set(target):
                errors.append(f"secret_targets[{index}] が不正")
            elif not set(target) <= _SECRET_TARGET_KEYS or _contains_secret_value(target):
                errors.append(f"secret_targets[{index}] に秘密の値または未定義の項目がある")
    if _contains_secret_value(entry):
        errors.append("台帳に秘密の値を書けない")
    return list(dict.fromkeys(errors))


def load_environment_mapping(document: Mapping[str, Any]) -> EnvironmentRegistry:
    """辞書から台帳を読む。壊れた環境だけを rejected として局所化する。"""
    supported = document.get("contract_supported")
    if isinstance(supported, str):
        supported = (supported,)
    elif isinstance(supported, list) and all(isinstance(item, str) for item in supported):
        supported = tuple(supported)
    else:
        raise RegistryError("contract_supported が無いか不正")
    rows = document.get("environments")
    if not isinstance(rows, list):
        raise RegistryError("environments の一覧が無い")
    entries: list[dict[str, Any]] = []
    rejected: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for index, item in enumerate(rows):
        environment_id = str(item.get("id", f"#{index}")) if isinstance(item, Mapping) else f"#{index}"
        problems = ["環境項目がオブジェクトでない"] if not isinstance(item, Mapping) else _entry_errors(item)
        if environment_id in seen:
            problems.append("id が重複している")
        seen.add(environment_id)
        if problems:
            rejected.append((environment_id, tuple(problems)))
        else:
            entries.append(dict(item))
    return EnvironmentRegistry(tuple(supported), tuple(entries), tuple(rejected))


def load_environment_file(path: str | os.PathLike | None = None) -> EnvironmentRegistry:
    """YAML 台帳を読む。資格の値や外部接続は読まない。"""
    import yaml

    source = Path(path) if path is not None else default_environment_path()
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as error:
        raise RegistryError(f"{source}: {error.strerror}") from None
    except yaml.YAMLError as error:
        raise RegistryError(f"{source}: YAML でない") from error
    if not isinstance(document, Mapping):
        raise RegistryError(f"{source}: 最上位をオブジェクトにする")
    return load_environment_mapping(document)
