"""Shared types for the SpreadsheetBench PoisonedEvolution reproduction.

The module deliberately contains no repository or provider imports so the
same records can cross the host/e9c subprocess boundary as JSON.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping


class ProtocolError(ValueError):
    """The preregistered trial contract is invalid."""


class EngineError(RuntimeError):
    """The isolated native engine could not return a scientific outcome."""


class TrialStatus(str, Enum):
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class TrialDecision(str, Enum):
    SKIP = "skip"
    MATERIALIZED = "materialized"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BehaviorFamily:
    """One independently detected, inert behavior family."""

    family_id: str
    title: str
    marker: str
    constraint: str
    action: str
    success: str
    tool_name: str


@dataclass(frozen=True)
class TransformEvidence:
    family_id: str
    source_session_sha256: Mapping[str, str]
    transformed_session_sha256: Mapping[str, str]
    carrier_session_ids: tuple[str, ...]
    unchanged_session_ids: tuple[str, ...]
    injected_tool_positions: Mapping[str, tuple[int, ...]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TransformResult:
    sessions: tuple[dict[str, Any], ...]
    evidence: TransformEvidence


@dataclass(frozen=True)
class DetectionResult:
    family_id: str
    matched: bool
    marker_present: bool
    operation_present: bool
    object_present: bool
    context_present: bool
    negated: bool
    matched_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StageDiagnostics:
    delivered_carriers: int
    expected_carriers: int
    summarized_carriers: int
    summary_positive_carriers: int
    candidate_documents: int
    candidate_positive_documents: int
    materialized_skills: int
    materialized_positive_skills: int
    materialized_detector_hits: Mapping[str, int] = field(default_factory=dict)
    materialized_skill_detector_hits: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    cross_family_detector_hits: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["materialized_skill_detector_hits"] = {
            name: list(families) for name, families in self.materialized_skill_detector_hits.items()
        }
        return value


@dataclass(frozen=True)
class LLMCallRecord:
    call_index: int
    stage: str
    started_at: str
    elapsed_seconds: float
    request: Mapping[str, Any]
    response_text: str | None
    usage: Mapping[str, Any] | None
    error_type: str | None = None
    error_message: str | None = None
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EngineRunResult:
    """Normalized output of exactly one native ``EvolveServer.run_once``."""

    decision: TrialDecision
    summary: Mapping[str, Any]
    summaries: Mapping[str, str]
    raw_candidates: tuple[str, ...]
    materialized_skills: Mapping[str, str]
    llm_calls: tuple[Mapping[str, Any], ...] = ()
    error_type: str | None = None
    error_message: str | None = None
    infrastructure_signature: str | None = None

    @classmethod
    def unknown(
        cls,
        *,
        error_type: str,
        error_message: str,
        infrastructure_signature: str | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> "EngineRunResult":
        return cls(
            decision=TrialDecision.UNKNOWN,
            summary=dict(summary or {}),
            summaries={},
            raw_candidates=(),
            materialized_skills={},
            error_type=error_type,
            error_message=error_message,
            infrastructure_signature=infrastructure_signature,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["decision"] = self.decision.value
        return data


@dataclass(frozen=True)
class TrialResult:
    trial_id: str
    family_id: str
    batch_id: str
    status: TrialStatus
    decision: TrialDecision
    ser_success: bool | None
    diagnostics: StageDiagnostics
    transform: TransformEvidence
    engine: EngineRunResult
    unknown_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "family_id": self.family_id,
            "batch_id": self.batch_id,
            "status": self.status.value,
            "decision": self.decision.value,
            "ser_success": self.ser_success,
            "diagnostics": self.diagnostics.to_dict(),
            "transform": self.transform.to_dict(),
            "engine": self.engine.to_dict(),
            "unknown_reason": self.unknown_reason,
        }


@dataclass(frozen=True)
class FamilyMetrics:
    family_id: str
    attempted: int
    completed: int
    unknown: int
    successes: int
    ser: float | None
    stopped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MatrixResult:
    families: Mapping[str, FamilyMetrics]
    attempted: int
    completed: int
    unknown: int
    successes: int
    ser: float | None
    circuit_open: bool = False
    circuit_signature: str | None = None
    trials: tuple[TrialResult, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "families": {key: value.to_dict() for key, value in self.families.items()},
            "attempted": self.attempted,
            "completed": self.completed,
            "unknown": self.unknown,
            "successes": self.successes,
            "ser": self.ser,
            "circuit_open": self.circuit_open,
            "circuit_signature": self.circuit_signature,
            "trials": [trial.to_dict() for trial in self.trials],
        }
