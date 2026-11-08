"""Fixed-target, tool-free publication reviews for S-20.7.

Model selection and execution are injected by S-8.  This module never keeps a
model preference list and it never retains model output.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from axcommon.schemas import validate


RECORD_SCHEMA = "ext/b15/gate/publication-review"
STAGES = ("stage-1", "stage-2", "stage-3")
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class SourceFile:
    """One file in the exact publication material."""

    path: str
    text: str
    binary: bytes | None = None


@dataclass(frozen=True)
class PublicationTarget:
    """The fixed publication material and the repository history to inspect."""

    target_ref: str
    version: str
    attempt: int
    files: tuple[SourceFile, ...]
    history: str
    sensitivity: str = "public"

    @classmethod
    def from_files(
        cls,
        *,
        target_ref: str,
        version: str,
        attempt: int,
        files: Mapping[str, str] | Iterable[tuple[str, str]],
        history: str,
        sensitivity: str = "public",
    ) -> "PublicationTarget":
        pairs = files.items() if isinstance(files, Mapping) else files
        frozen = tuple(SourceFile(path, '[binary image]' if isinstance(text, bytes) else text,
                                  text if isinstance(text, bytes) else None) for path, text in pairs)
        return cls(target_ref, version, attempt, frozen, history, sensitivity)

    def material(self) -> str:
        """Return every target file in a stable, reviewable representation."""
        return "".join(f"\n--- {item.path} ---\n{item.text}" for item in self.files)

    def is_complete(self) -> bool:
        return (
            bool(self.target_ref)
            and bool(self.version)
            and type(self.attempt) is int
            and self.attempt > 0
            and bool(self.files)
            and isinstance(self.history, str)
            and all(isinstance(item.path, str) and item.path and isinstance(item.text, str) for item in self.files)
            and self.sensitivity in {"public", "private", "sensitive"}
        )


@dataclass(frozen=True)
class ReviewChunk:
    """A complete-coverage material fragment with overlap decided by the gate."""

    id: str
    text: str


@dataclass(frozen=True)
class ReviewRequest:
    """The only request shape handed from the gate to the S-8 selector."""

    target_ref: str
    version: str
    attempt: int
    stage: str
    chunk: ReviewChunk
    sensitivity: str
    purpose: str = "publication_review"
    role: str = "review"
    independent: bool = True
    tools_enabled: bool = False
    mcp_enabled: bool = False
    memory_enabled: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "role": self.role,
            "sensitivity": self.sensitivity,
            "target_ref": self.target_ref,
            "version": self.version,
            "attempt": self.attempt,
            "stage": self.stage,
            "chunk": {"id": self.chunk.id, "text": self.chunk.text},
            "independent": self.independent,
            "tools": [],
            "tools_enabled": self.tools_enabled,
            "mcp_enabled": self.mcp_enabled,
            "memory_enabled": self.memory_enabled,
        }


@dataclass(frozen=True)
class SelectedReviewer:
    """A single S-8-selected reviewer.  Its identity is never written to records."""

    id: str
    call: Callable[[dict[str, Any]], object]


class ReviewerSelector(Protocol):
    def select(self, request: ReviewRequest) -> SelectedReviewer | None: ...


class ReviewStore(Protocol):
    def append(self, record: dict[str, Any]) -> str: ...


class TargetScanner(Protocol):
    def __call__(self, target: PublicationTarget) -> bool: ...


@dataclass(frozen=True)
class GateResult:
    ok: bool
    target_ref: str
    version: str
    attempt: int
    material_bound: bool
    evidence_ref: str | None
    outcome: str
    reason: str
    identity_required: bool = False
    leak_detected: bool = False
    record: dict[str, Any] | None = None

    def as_release_port(self) -> dict[str, Any]:
        """Return the existing ``Ports.gate`` response without review text."""
        return {
            "ok": self.ok,
            "target_sha": self.target_ref,
            "material_bound": self.material_bound,
            "evidence_ref": self.evidence_ref,
            "outcome": self.outcome,
            "reason": self.reason,
            "identity_required": self.identity_required,
            "leak_detected": self.leak_detected,
        }


class JsonlReviewStore:
    """Append only the S-20.7-safe record, never target material or model output."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, record: dict[str, Any]) -> str:
        validate(RECORD_SCHEMA, record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
        return f"publish-review:{record['target_ref']}:{record['attempt']}"


def split_material(text: str, *, chunk_chars: int, overlap_chars: int) -> tuple[ReviewChunk, ...]:
    """Split all material into overlapping chunks; no text is dropped."""
    if type(chunk_chars) is not int or type(overlap_chars) is not int or chunk_chars < 1 or overlap_chars < 0:
        raise ValueError("invalid chunk sizing")
    if overlap_chars >= chunk_chars:
        raise ValueError("overlap must be smaller than a chunk")
    if not text:
        return (ReviewChunk("chunk-0001", ""),)
    step = chunk_chars - overlap_chars
    return tuple(
        ReviewChunk(f"chunk-{index:04d}", text[start:start + chunk_chars])
        for index, start in enumerate(range(0, len(text), step), start=1)
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _model_outcome(value: object) -> tuple[str, bool]:
    """Parse only the decision fields; raw model output is deliberately discarded."""
    if not isinstance(value, Mapping):
        return "unsure", False
    outcome = value.get("outcome")
    if outcome not in {"ok", "ng", "unsure"}:
        return "unsure", False
    return outcome, value.get("identity_required") is True


class PublicationGate:
    """Execute the mechanical and independent three-stage review contract."""

    def __init__(
        self,
        *,
        redactor: Any,
        pii_list_available: bool,
        internal_information_check: TargetScanner,
        image_gps_check: TargetScanner,
        selector: ReviewerSelector,
        records: ReviewStore,
        clock: Callable[[], str] = _now,
        chunk_chars: int = 6000,
        overlap_chars: int = 300,
    ):
        self.redactor = redactor
        self.pii_list_available = pii_list_available
        self.internal_information_check = internal_information_check
        self.image_gps_check = image_gps_check
        self.selector = selector
        self.records = records
        self.clock = clock
        self.chunk_chars = chunk_chars
        self.overlap_chars = overlap_chars

    def run(self, target: PublicationTarget, *, expected_target_ref: str | None = None) -> GateResult:
        if not target.is_complete() or (expected_target_ref is not None and target.target_ref != expected_target_ref):
            return self._complete(target, [], "failed", "target_binding_invalid")
        if target.attempt > MAX_ATTEMPTS:
            return self._complete(target, [], "failed", "attempt_limit")

        checks, reason, leak_detected = self._mechanical_checks(target)
        if reason is not None:
            return self._complete(target, checks, "failed", reason, leak_detected=leak_detected)

        identity_required = False
        for chunk in split_material(target.material(), chunk_chars=self.chunk_chars, overlap_chars=self.overlap_chars):
            for stage in STAGES:
                request = ReviewRequest(
                    target_ref=target.target_ref,
                    version=target.version,
                    attempt=target.attempt,
                    stage=stage,
                    chunk=chunk,
                    sensitivity=target.sensitivity,
                )
                outcome, needs_identity = self._run_review(request)
                identity_required = identity_required or needs_identity
                checks.append({"kind": "model", "stage": stage, "chunk": chunk.id, "outcome": outcome})

        outcomes = [entry["outcome"] for entry in checks if entry["kind"] == "model"]
        if identity_required:
            return self._complete(target, checks, "parked", "identity_required", identity_required=True)
        if any(outcome != "ok" for outcome in outcomes):
            reason = "review_ng" if "ng" in outcomes else "review_unsure"
            return self._complete(target, checks, "failed", reason)
        return self._complete(target, checks, "passed", "passed")

    def _mechanical_checks(self, target: PublicationTarget) -> tuple[list[dict[str, str]], str | None, bool]:
        checks: list[dict[str, str]] = []
        if self.pii_list_available is not True:
            checks.append({"kind": "machine", "name": "secret_pii", "outcome": "unsure"})
            return checks, "pii_list_missing", False
        try:
            if hasattr(self.redactor, "scan_findings"):
                findings = self.redactor.scan_findings(target)
                hits = bool(findings)
            else:
                hits = self.redactor.scan(target.material() + "\n" + target.history + "\n" +
                                          "\n".join(item.binary.decode("latin-1") for item in target.files
                                                     if item.binary is not None))
                findings = self.redactor.findings(target) if hits and hasattr(self.redactor, "findings") else []
        except Exception:
            checks.append({"kind": "machine", "name": "secret_pii", "outcome": "unsure"})
            return checks, "machine_check_unavailable", False
        if hits and (not findings or any(f.get("status") != "allowed" for f in findings)):
            check = {"kind": "machine", "name": "secret_pii", "outcome": "ng"}
            if findings:
                check["findings"] = findings
            checks.append(check)
            return checks, "secret_or_pii_detected", True
        check = {"kind": "machine", "name": "secret_pii", "outcome": "ok"}
        if findings:
            check["findings"] = findings
        checks.append(check)

        for name, scanner, detected_reason in (
            ("internal_information", self.internal_information_check, "internal_information_detected"),
            ("image_gps", self.image_gps_check, "image_gps_detected"),
        ):
            try:
                clear = scanner(target)
                findings = scanner.findings(target) if hasattr(scanner, "findings") else []
            except Exception:
                checks.append({"kind": "machine", "name": name, "outcome": "unsure"})
                return checks, "machine_check_unavailable", False
            if clear is not True:
                check = {"kind": "machine", "name": name, "outcome": "ng"}
                if findings:
                    check["findings"] = findings
                checks.append(check)
                return checks, detected_reason, True
            check = {"kind": "machine", "name": name, "outcome": "ok"}
            if findings:
                check["findings"] = findings
            checks.append(check)
        return checks, None, False

    def _run_review(self, request: ReviewRequest) -> tuple[str, bool]:
        try:
            reviewer = self.selector.select(request)
            if reviewer is None or not reviewer.id:
                return "unsure", False
            response = reviewer.call(request.payload())
        except Exception:
            return "unsure", False
        return _model_outcome(response)

    def _complete(
        self,
        target: PublicationTarget,
        checks: list[dict[str, str]],
        outcome: str,
        reason: str,
        *,
        identity_required: bool = False,
        leak_detected: bool = False,
    ) -> GateResult:
        record = {
            "target_ref": target.target_ref if target.target_ref else "[REDACTED]",
            "version": target.version if target.version else "[REDACTED]",
            "attempt": target.attempt if type(target.attempt) is int else 0,
            "checks": deepcopy(checks),
            "outcome": outcome,
            "reason": reason,
            "at": self.clock(),
        }
        try:
            validate(RECORD_SCHEMA, record)
            evidence_ref = self.records.append(deepcopy(record))
        except Exception:
            return GateResult(
                False,
                record["target_ref"],
                record["version"],
                record["attempt"],
                False,
                None,
                "failed",
                "record_unavailable",
                identity_required=identity_required,
                leak_detected=leak_detected,
                record=record,
            )
        return GateResult(
            outcome == "passed",
            record["target_ref"],
            record["version"],
            record["attempt"],
            outcome == "passed",
            evidence_ref,
            outcome,
            reason,
            identity_required=identity_required,
            leak_detected=leak_detected,
            record=record,
        )


class ReleaseGateAdapter:
    """The ``ReleaseService.Ports.gate`` adapter for the B15 publication path."""

    def __init__(
        self,
        gate: PublicationGate,
        target_factory: Callable[[dict[str, Any], str, dict[str, Any]], PublicationTarget],
        *,
        create_identity_card: Callable[[dict[str, Any]], bool] | None = None,
    ):
        self.gate_runner = gate
        self.target_factory = target_factory
        self.create_identity_card = create_identity_card

    def gate(self, product: dict[str, Any], sha: str, notes: dict[str, Any]) -> dict[str, Any]:
        try:
            target = self.target_factory(deepcopy(product), sha, deepcopy(notes))
            result = self.gate_runner.run(target, expected_target_ref=sha)
        except Exception:
            return {
                "ok": False,
                "target_sha": sha,
                "material_bound": False,
                "evidence_ref": None,
                "outcome": "failed",
                "reason": "publication_target_unavailable",
                "identity_required": False,
                "leak_detected": False,
            }
        if result.identity_required:
            if self.create_identity_card is None or self.create_identity_card({
                "target_ref": result.target_ref,
                "version": result.version,
                "attempt": result.attempt,
                "reason": "identity_required",
            }) is not True:
                return {**result.as_release_port(), "reason": "identity_card_unavailable"}
        return result.as_release_port()
