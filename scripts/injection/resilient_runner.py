"""Versioned scheduling and recovery primitives for new trajectory experiments.

The helpers in this module are deliberately independent from the frozen
historical runners.  They execute isolated cases through caller supplied
callbacks, persist enough state to resume, and never turn a missing result into
zero or silently replace an incompatible baseline with a fresh execution.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Literal


VERSION = "resilient-v1"
QUEUE_SCHEMA = "resilient-case-queue-v1"
BASELINE_SCHEMA = "same-stage-day-baseline-import-v2"
LEGACY_BASELINE_SCHEMA = "same-stage-day-baseline-import-v1"
EXECUTION_IDENTITY_FIELDS = (
    "model_id", "model_revision", "temperature", "top_p", "top_k", "min_p",
    "presence_penalty", "repetition_penalty", "reasoning_effort", "max_tokens", "context_length", "seed",
    "enable_thinking", "chat_template_kwargs", "required_shared_skill",
    "task_timeout_seconds", "grading_timeout_seconds", "grade_timeout_seconds", "native_llm_request_timeout_seconds",
    "external_judge", "judge_model_id", "judge_reasoning_effort", "judge_max_tokens",
    "local_judge_enable_thinking", "local_judge_max_tokens",
)
JUDGE_IDENTITY_FIELDS = frozenset({"grading_timeout_seconds", "grade_timeout_seconds", "external_judge",
    "judge_model_id", "judge_model_revision", "judge_reasoning_effort", "judge_max_tokens",
    "local_judge_enable_thinking", "local_judge_max_tokens"})
AGENT_REUSE_FIELDS = tuple(key for key in EXECUTION_IDENTITY_FIELDS if key not in JUDGE_IDENTITY_FIELDS) + (
    "alignment", "benchmark_revision", "dataset_revision", "audio_model_revision", "image_model_revision",
    "task_image_id", "initial_skill_names", "case_timeout_seconds", "model_context_window",
)
DERIVED_SOURCE_SCHEMA = "verified-agent-reuse-with-independent-grading-v1"
INFRASTRUCTURE_SCHEMA = "resilient-infrastructure-circuit-v1"
MAX_CONCURRENCY = 32
ARCHIVE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class ProtocolError(ValueError):
    """The requested resilient protocol is incomplete or incompatible."""


class StageBudgetExceeded(RuntimeError):
    """A frozen stage wall-clock deadline has elapsed."""


class CircuitBreakerOpen(RuntimeError):
    """Two consecutive infrastructure failures had the same signature."""


def _canonical(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def execution_protocol(config: dict) -> dict | None:
    """Return a validated protocol only when the new version is explicit."""
    value = config.get("execution_protocol")
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("version") != VERSION:
        raise ProtocolError("Unknown execution_protocol version")
    conditions = value.get("conditions", config.get("conditions"))
    if (not isinstance(conditions, list) or not conditions
            or len(set(conditions)) != len(conditions)
            or any(condition not in {"clean", "poisoned"} for condition in conditions)):
        raise ProtocolError("resilient-v1 conditions must contain unique clean/poisoned branches")
    configured_conditions = config.get("conditions")
    if configured_conditions is not None and conditions != configured_conditions:
        raise ProtocolError("execution_protocol conditions differ from the experiment conditions")
    workers = value.get("max_concurrency")
    if workers is None:
        day_workers, validation_workers = value.get("day_workers"), value.get("validation_workers")
        if day_workers != validation_workers:
            raise ProtocolError("resilient-v1 currently requires equal Day and validation worker limits")
        workers = day_workers
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= MAX_CONCURRENCY:
        raise ProtocolError(f"execution_protocol.max_concurrency must be 1..{MAX_CONCURRENCY}")
    retries = value.get("max_transient_retries", value.get("infrastructure_retries_per_case"))
    if retries != 1:
        raise ProtocolError("resilient-v1 permits exactly one transient retry")
    if value.get("task_scientific_retries", 0) != 0:
        raise ProtocolError("resilient-v1 forbids scientific retries")
    if config.get("infrastructure_retries", 0) != 0:
        raise ProtocolError("resilient-v1 owns retries; infrastructure_retries must be zero")
    breaker = value.get("circuit_breaker")
    if breaker is None:
        breaker = {"same_signature_limit": value.get("identical_infrastructure_error_circuit_breaker")}
    if not isinstance(breaker, dict) or breaker.get("same_signature_limit") != 2:
        raise ProtocolError("resilient-v1 requires a two-signature infrastructure circuit breaker")
    budgets = value.get("stage_budgets", config.get("stage_budgets"))
    if budgets is None and isinstance(config.get("budgets"), dict):
        declared = config["budgets"]
        budgets = {"night_seconds": declared.get("round_wallclock_hard_cap_seconds")}
        if declared.get("final_utility_wallclock_hard_cap_seconds"):
            budgets["endpoint_seconds"] = declared["final_utility_wallclock_hard_cap_seconds"]
    if not isinstance(budgets, dict) or "night_seconds" not in budgets:
        raise ProtocolError("resilient-v1 requires stage_budgets.night_seconds")
    for name, seconds in budgets.items():
        if not name.endswith("_seconds") or isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            raise ProtocolError(f"Invalid stage budget: {name}")
    baseline = value.get("baseline_import", config.get("baseline_import"))
    if baseline is not None:
        if not isinstance(baseline, dict) or set(baseline) != {"import_bundle_dir"}:
            raise ProtocolError("baseline_import must contain only import_bundle_dir")
        directory = Path(baseline["import_bundle_dir"])
        if not directory.is_absolute():
            raise ProtocolError("baseline_import.import_bundle_dir must be absolute")
    return {**value, "conditions": conditions, "max_concurrency": workers, "max_transient_retries": retries,
            "circuit_breaker": breaker, "stage_budgets": budgets, "baseline_import": baseline}


def enabled(config: dict) -> bool:
    return execution_protocol(config) is not None


def typed_source_refs(task_ids: Iterable[str], session_ids: Iterable[str]) -> list[dict[str, str]]:
    """Build an unambiguous, canonical source identity list."""
    tasks, sessions = sorted(set(task_ids)), sorted(set(session_ids))
    if any(not isinstance(value, str) or not value for value in [*tasks, *sessions]):
        raise ProtocolError("Source identifiers must be nonempty strings")
    if set(tasks) & set(sessions):
        raise ProtocolError("A source identifier cannot be both a task and a session")
    return ([{"kind": "task", "id": value} for value in tasks]
            + [{"kind": "session", "id": value} for value in sessions])


def validate_typed_sources(candidate: dict) -> list[dict[str, str]]:
    expected = typed_source_refs(candidate.get("source_task_ids", []), candidate.get("source_session_ids", []))
    actual = candidate.get("source_refs")
    if actual != expected:
        raise ProtocolError("Candidate typed source_refs differ from source_task_ids/source_session_ids")
    return actual


@dataclass(frozen=True)
class Case:
    key: str
    identity: dict
    payload: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key or "/" in self.key or ".." in self.key:
            raise ProtocolError("Case keys must be nonempty path-safe strings")
        if not isinstance(self.identity, dict) or not self.identity:
            raise ProtocolError("Each case needs a frozen identity")


CaseStatus = Literal["completed", "transient", "terminal_transient", "failed"]


@dataclass(frozen=True)
class Classification:
    status: CaseStatus
    error_type: str | None = None
    error: str | None = None


_TRANSIENT = re.compile(
    r"(?:connection(?:refused|reset|error)?|transport|bad gateway|service unavailable|gateway timeout|"
    r"\b50[234]\b|\b429\b|rate limit|readtimeout|connecttimeout|timed out|\btimeout\b|"
    r"temporar(?:y|ily) unavailable|runtime child exited)",
    re.IGNORECASE,
)


def infrastructure_signature(error_type: str | None, error: str | None) -> str | None:
    """Return a stable signature only for known transient infrastructure failures."""
    text = " ".join(part for part in (error_type, error) if part)
    if not text or not _TRANSIENT.search(text):
        return None
    normalized = text.lower()
    normalized = re.sub(r"https?://[^\s]+", "<url>", normalized)
    normalized = re.sub(r"(?:/[A-Za-z0-9_.-]+){2,}", "<path>", normalized)
    normalized = re.sub(r"\b[0-9a-f]{12,}\b", "<id>", normalized)
    normalized = re.sub(r"\b\d+\b", "<n>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


def observe_infrastructure_result(
    checkpoint: Path, unit: str, *, error_type: str | None = None, error: str | None = None,
) -> dict:
    """Persist a stage-wide infrastructure streak and open on two equal signatures.

    Calls are idempotent by ``unit`` so cached preflights can be inspected again
    during resume without incrementing the breaker twice. A non-infrastructure
    result clears the consecutive streak.
    """
    if not isinstance(unit, str) or not unit:
        raise ProtocolError("Infrastructure observations require a stable unit identity")
    signature = infrastructure_signature(error_type, error)
    state = _read_json(checkpoint, {})
    if state and state.get("schema") != INFRASTRUCTURE_SCHEMA:
        raise ProtocolError("Unknown infrastructure circuit checkpoint schema")
    if not state:
        state = {"schema": INFRASTRUCTURE_SCHEMA, "protocol_version": VERSION,
                 "events": {}, "streak": {"signature": None, "count": 0}, "circuit": None}
    events = state.get("events")
    if not isinstance(events, dict):
        raise ProtocolError("Infrastructure circuit events are invalid")
    if unit in events:
        prior = events[unit]
        if prior.get("signature") != signature:
            raise ProtocolError("Infrastructure observation changed during resume")
        if state.get("circuit") is not None:
            raise CircuitBreakerOpen(
                f"Infrastructure circuit already open: {state['circuit']['signature']}")
        return prior
    if state.get("circuit") is not None:
        raise CircuitBreakerOpen(f"Infrastructure circuit already open: {state['circuit']['signature']}")
    streak = state.get("streak") or {}
    if signature is None:
        streak = {"signature": None, "count": 0}
    elif streak.get("signature") == signature:
        streak = {"signature": signature, "count": int(streak.get("count", 0)) + 1}
    else:
        streak = {"signature": signature, "count": 1}
    event = {"unit": unit, "signature": signature, "error_type": error_type,
             "error": error, "observed_at": datetime.now(timezone.utc).isoformat()}
    events[unit] = event
    state["streak"] = streak
    if signature is not None and streak["count"] >= 2:
        state["circuit"] = {"status": "open", "signature": signature,
                            "consecutive_failures": streak["count"], "opened_by": unit}
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(checkpoint, state)
    if state.get("circuit") is not None:
        raise CircuitBreakerOpen(f"Infrastructure circuit opened: {signature}")
    return event


def classify_result(result: Any) -> Classification:
    """Classify the result shapes used by both Day and validation workers."""
    value = result
    if isinstance(value, dict) and "state" in value:
        value = value["state"]
    if isinstance(value, dict) and value.get("status") == "completed":
        score = value.get("score") if "score" in value else value.get("task_score")
        if (not isinstance(score, bool) and isinstance(score, (int, float))
                and math.isfinite(score)):
            return Classification("completed")
    error_type = value.get("error_type") if isinstance(value, dict) else type(value).__name__
    error = value.get("error") if isinstance(value, dict) else str(value)
    status = value.get("status") if isinstance(value, dict) else None
    signature = infrastructure_signature(error_type, error)
    if status in {"infrastructure_error", "failed_or_incomplete", "pending_score"} and signature:
        return Classification("transient", error_type, error)
    return Classification("failed", error_type, error or f"Non-complete result status: {status}")


def _exception_result(error: BaseException) -> dict:
    return {"status": "failed_or_incomplete", "error_type": type(error).__name__, "error": str(error)}


def run_case_queue(
    cases: Iterable[Case], execute: Callable[[Case], Any], checkpoint: Path, *,
    max_workers: int, classify: Callable[[Any], Classification] = classify_result,
    max_transient_retries: int = 1, same_signature_limit: int = 2,
    budget: "StageBudget | None" = None,
    recover_interrupted: Callable[[Case, dict], bool] | None = None,
) -> dict[str, dict]:
    """Run independent cases with durable resume, bounded retry and a circuit breaker.

    Non-infrastructure case failures are recorded and do not prevent unrelated
    cases from running.  Once the same infrastructure signature occurs twice in
    succession, no further cases are submitted.  Already-running callbacks are
    allowed to finish so their original evidence is not killed mid-write.  On
    a later invocation, terminal failures stay sealed.  A case left ``running``
    by an interrupted process is resumed only when the caller can prove that it
    reuses a completed result or a frozen Agent output.
    """
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= MAX_CONCURRENCY:
        raise ProtocolError(f"max_workers must be 1..{MAX_CONCURRENCY}")
    if max_transient_retries != 1 or same_signature_limit != 2:
        raise ProtocolError("resilient-v1 fixes retry=1 and same-signature-limit=2")
    supplied = list(cases)
    if len({case.key for case in supplied}) != len(supplied):
        raise ProtocolError("Case keys must be unique")
    identities = {case.key: _canonical(case.identity) for case in supplied}
    state = _read_json(checkpoint, {})
    loaded_checkpoint = bool(state)
    if state:
        if state.get("schema") != QUEUE_SCHEMA:
            raise ProtocolError("Unknown queue checkpoint schema")
        saved = state.get("identities", {})
        if saved != identities:
            raise ProtocolError("Queue checkpoint belongs to different case identities")
    else:
        state = {"schema": QUEUE_SCHEMA, "protocol_version": VERSION, "identities": identities,
                 "cases": {}, "circuit": None, "updated_at": None}
    records: dict[str, dict] = state["cases"]
    resumed_checkpoint = loaded_checkpoint
    resume_changed = False
    if resumed_checkpoint:
        prior_circuit = state.get("circuit")
        if prior_circuit is not None:
            history = state.setdefault("circuit_history", [])
            history.append({**prior_circuit, "reset_at": datetime.now(timezone.utc).isoformat()})
            state["circuit"] = None
            state["infrastructure_streak"] = {"signature": None, "consecutive_failures": 0}
            resume_changed = True
        for case in supplied:
            record = records.get(case.key)
            if not isinstance(record, dict):
                continue
            status = record.get("status")
            attempts_used = int(record.get("attempts", 0))
            if status == "running":
                safe = (
                    attempts_used <= max_transient_retries
                    and recover_interrupted is not None
                    and bool(recover_interrupted(case, record))
                )
                if safe:
                    record["status"] = "resume_pending"
                    record["resume_reason"] = "caller_verified_reuse_or_frozen_output"
                else:
                    record["status"] = "failed"
                    record["reason"] = "interrupted_after_case_submission_agent_reexecution_forbidden"
                resume_changed = True
            elif status in {"transient_failure", "circuit_open"} and attempts_used > 0:
                queued = record.get("result")
                retry_safe = (
                    isinstance(queued, dict)
                    and queued.get("infrastructure_retry_eligible") is True
                    and attempts_used <= max_transient_retries
                )
                if retry_safe:
                    record["status"] = "resume_pending"
                    record["resume_reason"] = "pre_agent_retry_or_frozen_score_recovery"
                else:
                    record["status"] = "failed"
                    record["reason"] = "prior_attempt_not_safe_to_resume"
                resume_changed = True
            elif status == "failed" and attempts_used > 0:
                queued = record.get("result")
                frozen_score = (
                    record.get("reason") == "case_failure"
                    and isinstance(queued, dict)
                    and queued.get("status") == "pending_score"
                    and attempts_used <= max_transient_retries
                    and recover_interrupted is not None
                    and bool(recover_interrupted(case, record))
                )
                if frozen_score:
                    record["status"] = "resume_pending"
                    record["resume_reason"] = "caller_verified_frozen_output_score_recovery"
                    resume_changed = True
            elif status == "circuit_open" and attempts_used == 0:
                record["status"] = "resume_pending"
                record["resume_reason"] = "case_never_started_before_circuit_opened"
                resume_changed = True
    pending = [case for case in supplied if records.get(case.key, {}).get("status") not in {"completed", "failed"}]
    attempts = {case.key: int(records.get(case.key, {}).get("attempts", 0)) for case in supplied}
    streak = state.get("infrastructure_streak") or {}
    last_signature = streak.get("signature")
    consecutive = int(streak.get("consecutive_failures", 0))
    circuit: dict | None = state.get("circuit")

    def save() -> None:
        state.update(cases=records, circuit=circuit,
                     infrastructure_streak={"signature": last_signature,
                                            "consecutive_failures": consecutive},
                     updated_at=datetime.now(timezone.utc).isoformat())
        _write_json(checkpoint, state)

    if resume_changed:
        save()

    def invoke(case: Case) -> tuple[Any, Classification]:
        if budget is not None:
            budget.require_remaining(f"case:{case.key}")
        try:
            result = execute(case)
        except (StageBudgetExceeded, CircuitBreakerOpen):
            raise
        except Exception as error:
            result = _exception_result(error)
        return result, classify(result)

    queue = list(pending)
    active: dict[Future, Case] = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="isolated-case") as pool:
        while queue or active:
            while queue and len(active) < max_workers and circuit is None:
                case = queue.pop(0)
                attempts[case.key] += 1
                records[case.key] = {"status": "running", "attempts": attempts[case.key],
                                     "identity_sha256": identities[case.key]}
                save()
                active[pool.submit(invoke, case)] = case
            if not active:
                break
            finished, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in finished:
                case = active.pop(future)
                result, classification = future.result()
                signature = infrastructure_signature(classification.error_type, classification.error)
                if classification.status == "completed":
                    last_signature, consecutive = None, 0
                    records[case.key] = {"status": "completed", "attempts": attempts[case.key],
                                         "identity_sha256": identities[case.key], "result": result}
                elif classification.status in {"transient", "terminal_transient"}:
                    if signature == last_signature:
                        consecutive += 1
                    else:
                        last_signature, consecutive = signature, 1
                    records[case.key] = {"status": "transient_failure", "attempts": attempts[case.key],
                        "identity_sha256": identities[case.key], "result": result,
                        "infrastructure_signature": signature}
                    if signature and consecutive >= same_signature_limit:
                        circuit = {"status": "open", "signature": signature,
                                   "consecutive_failures": consecutive, "opened_by": case.key}
                        if classification.status == "terminal_transient":
                            records[case.key]["status"] = "failed"
                            records[case.key]["reason"] = "transient_retry_unsafe_after_agent_started"
                        else:
                            records[case.key]["status"] = "circuit_open"
                            records[case.key]["reason"] = circuit
                    elif (classification.status == "transient"
                          and attempts[case.key] <= max_transient_retries):
                        queue.append(case)
                    else:
                        records[case.key]["status"] = "failed"
                        records[case.key]["reason"] = (
                            "transient_retry_unsafe_after_agent_started"
                            if classification.status == "terminal_transient"
                            else "transient_retry_exhausted"
                        )
                else:
                    last_signature, consecutive = None, 0
                    records[case.key] = {"status": "failed", "attempts": attempts[case.key],
                        "identity_sha256": identities[case.key], "result": result,
                        "reason": "case_failure"}
                save()
    if circuit is not None:
        for case in queue:
            previous = records.get(case.key, {})
            records[case.key] = {**previous, "status": "circuit_open", "attempts": attempts[case.key],
                "identity_sha256": identities[case.key], "reason": circuit}
        save()
    return {case.key: records[case.key] for case in supplied}


@dataclass(frozen=True)
class StageBudget:
    path: Path
    stage: str
    limit_seconds: float
    started_epoch: float
    deadline_epoch: float

    @classmethod
    def open(cls, path: Path, stage: str, limit_seconds: float, *, clock: Callable[[], float] = time.time) -> "StageBudget":
        if isinstance(limit_seconds, bool) or not isinstance(limit_seconds, (int, float)) or limit_seconds <= 0:
            raise ProtocolError("Stage budget must be a positive number of seconds")
        identity = {"protocol_version": VERSION, "stage": stage, "limit_seconds": float(limit_seconds)}
        saved = _read_json(path)
        if saved:
            if saved.get("identity_sha256") != _canonical(identity):
                raise ProtocolError("Stage budget checkpoint belongs to another stage or limit")
            started, deadline = float(saved["started_epoch"]), float(saved["deadline_epoch"])
        else:
            started = float(clock())
            deadline = started + float(limit_seconds)
            _write_json(path, {**identity, "identity_sha256": _canonical(identity),
                "started_epoch": started, "deadline_epoch": deadline,
                "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat()})
        return cls(path, stage, float(limit_seconds), started, deadline)

    def remaining(self, *, clock: Callable[[], float] = time.time) -> float:
        return max(0.0, self.deadline_epoch - float(clock()))

    def require_remaining(self, component: str, *, clock: Callable[[], float] = time.time) -> float:
        remaining = self.remaining(clock=clock)
        if remaining <= 0:
            raise StageBudgetExceeded(f"Stage budget exhausted before {component}: {self.stage}")
        return remaining

    def cap_config(self, config: dict, component: str, *, clock: Callable[[], float] = time.time) -> dict:
        """Cap every underlying timeout by the one shared persisted deadline."""
        remaining = self.require_remaining(component, clock=clock)
        cap = max(1, int(math.floor(remaining)))
        result = dict(config)
        for field in ("agent_timeout", "evolution_timeout_seconds", "task_timeout_seconds",
                      "grading_timeout_seconds", "native_llm_request_timeout_seconds"):
            if field in result:
                result[field] = min(int(result[field]), cap)
        result["stage_budget"] = {"protocol_version": VERSION, "stage": self.stage,
            "path": str(self.path.resolve()), "deadline_epoch": self.deadline_epoch,
            "limit_seconds": self.limit_seconds}
        return result


def budget_from_config(config: dict, stage: str, directory: Path) -> StageBudget | None:
    protocol = execution_protocol(config)
    if protocol is None:
        return None
    key = stage.split(":", 1)[0] + "_seconds"
    seconds = protocol["stage_budgets"].get(key)
    if seconds is None:
        return None
    return StageBudget.open(directory / f"{stage.replace(':', '-')}-budget.json", stage, seconds)


def active_budget(config: dict) -> StageBudget | None:
    descriptor = config.get("stage_budget")
    if descriptor is None:
        return None
    if (not isinstance(descriptor, dict) or descriptor.get("protocol_version") != VERSION
            or not Path(str(descriptor.get("path", ""))).is_absolute()):
        raise ProtocolError("Invalid active stage budget descriptor")
    budget = StageBudget.open(Path(descriptor["path"]), descriptor["stage"], descriptor["limit_seconds"])
    if budget.deadline_epoch != descriptor.get("deadline_epoch"):
        raise ProtocolError("Active stage budget deadline differs from its checkpoint")
    return budget


def baseline_import(config: dict) -> "BaselineImport | None":
    protocol = execution_protocol(config)
    if protocol is None or protocol.get("baseline_import") is None:
        return None
    return BaselineImport(Path(protocol["baseline_import"]["import_bundle_dir"]))


def baseline_execution_identity(config: dict, tasks: Iterable[dict], bank_sha256: str) -> dict:
    task_rows = [{key: task.get(key) for key in (
        "task_id", "short_id", "user_id", "source_sha256", "input_sha256", "timeout_seconds"
    )} for task in tasks]
    identity = {
        "identity_version": "baseline-execution-v2",
        "task_manifest_sha256": config.get("task_manifest_sha256", _canonical(task_rows)),
        "task_conditions_sha256": _canonical(task_rows),
        "s0_bank_sha256": bank_sha256,
        **{field: config.get(field) for field in EXECUTION_IDENTITY_FIELDS},
        "agent_prompt_sha256": config.get("agent_prompt_sha256"),
        "runtime_contract_sha256": config.get("runtime_contract_sha256"),
    }
    required = ("task_manifest_sha256", "s0_bank_sha256", "model_id", "model_revision",
                "agent_prompt_sha256", "runtime_contract_sha256")
    if any(not isinstance(identity[field], str) or not identity[field] for field in required):
        raise ProtocolError("Baseline execution identity lacks task, bank, model, prompt or runtime digests")
    return identity


def same_stage_day_baseline(stage: Path, task: dict, config: dict, bank_sha256: str) -> tuple[dict, dict]:
    """Verify and return the current stage's already-scored Day row."""
    stage = stage.resolve()
    state_path = stage / "day/tasks" / task["short_id"] / "state.json"
    state = _read_json(state_path, {})
    if state.get("status") != "completed" or state.get("task_id") != task["task_id"]:
        raise ProtocolError("Same-stage Day baseline is missing or incomplete")
    if state.get("bank_sha256") != bank_sha256:
        raise ProtocolError("Same-stage Day baseline belongs to a different bank")
    output = stage / state.get("task_output", f"day/tasks/{task['short_id']}/task")
    if (output.resolve() != output or not output.resolve().is_relative_to(stage)
            or not output.is_dir() or output.is_symlink()):
        raise ProtocolError("Same-stage Day output escapes its owner")
    result_path = output / "experiment-result.json"
    runtime_path = output / "runtime-config.json"
    binding_path = output / "execution-binding.json"
    result, runtime, binding = (_read_json(path, {}) for path in (result_path, runtime_path, binding_path))
    score = result.get("score")
    if (result.get("status") != "completed" or result.get("task_id") != task["task_id"]
            or isinstance(score, bool) or not isinstance(score, (int, float))
            or not math.isfinite(score) or not 0 <= score <= 1
            or score != state.get("task_score") or bool(result.get("timed_out")) != bool(state.get("timed_out"))):
        raise ProtocolError("Same-stage Day result is not a valid matching score")
    if (binding.get("bank_sha256") != bank_sha256
            or binding.get("protocol_hash") != config.get("protocol_hash")
            or result.get("run_id") != binding.get("run_id")):
        raise ProtocolError("Same-stage Day execution binding differs")
    expected_round = config.get("cycle_round", config.get("round"))
    runtime_task = runtime.get("task", {})
    runtime_round = runtime.get("cycle_round", runtime.get("round"))
    runtime_phase = runtime.get("phase", "day" if runtime.get("cycle_round") is not None else None)
    if (runtime_phase != "day" or runtime_round != expected_round
            or runtime_task.get("source_path") != task.get("source_path")
            or runtime.get("user_id") != task.get("user_id")):
        raise ProtocolError("Same-stage Day task/round identity differs")
    fields = (*EXECUTION_IDENTITY_FIELDS, "protocol_hash", "agent_prompt_sha256", "runtime_contract_sha256")
    if any(runtime.get(field) != config.get(field) for field in fields):
        raise ProtocolError("Same-stage Day model, sampling, prompt, runtime or budget identity differs")
    references = {}
    for name, path in (("state", state_path), ("result", result_path), ("runtime_config", runtime_path),
                       ("execution_binding", binding_path)):
        if path.is_symlink() or not path.is_file():
            raise ProtocolError("Same-stage Day provenance file is missing or symlinked")
        references[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    view = {**result, "short_id": task["short_id"], "user_id": task["user_id"],
            "category": task.get("category"), "skill_bank_sha256": bank_sha256}
    proof = {"policy": "same-stage-scored-day-v1", "reused_without_execution_or_rescoring": True,
        "source_record_relabelled": False, "new_model_calls": 0, "task_id": task["task_id"],
        "short_id": task["short_id"], "score": score, "timed_out": bool(result.get("timed_out")),
        "bank_sha256": bank_sha256, "sources": references}
    return view, proof


class BaselineImport:
    """Read a preflight-built Day import with an exact task set and sealed bytes."""

    IDENTITY_FIELDS = (
        "identity_version", "task_manifest_sha256", "task_conditions_sha256", "s0_bank_sha256",
        *EXECUTION_IDENTITY_FIELDS, "agent_prompt_sha256", "runtime_contract_sha256",
    )

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        if directory.is_symlink() or not self.directory.is_dir():
            raise ProtocolError("baseline_import bundle is missing or symlinked")
        self.manifest_path = self.directory / "manifest.json"
        if self.manifest_path.is_symlink() or not self.manifest_path.is_file():
            raise ProtocolError("baseline_import manifest is missing or symlinked")
        self.manifest = _read_json(self.manifest_path)
        if not isinstance(self.manifest, dict) or self.manifest.get("schema") not in {BASELINE_SCHEMA, LEGACY_BASELINE_SCHEMA}:
            raise ProtocolError("Unknown baseline import bundle schema")
        tasks = self.manifest.get("tasks")
        if (not isinstance(tasks, list) or not tasks
                or type(self.manifest.get("task_count")) is not int
                or self.manifest["task_count"] != len(tasks)):
            raise ProtocolError("Baseline import task_count must match its nonempty task set")
        self.tasks = {item.get("task_id"): item for item in tasks}
        if None in self.tasks or len(self.tasks) != len(tasks):
            raise ProtocolError("Baseline import task identities are missing or duplicated")
        for item in tasks:
            for reference in [item.get("result"), *self._session_references(item)]:
                if not isinstance(reference, dict):
                    raise ProtocolError("Baseline task lacks a result/session reference")
                self._reference(reference)

    @staticmethod
    def _session_references(item: dict) -> list[dict]:
        if "sessions" not in item:
            return [item.get("session")]
        rows = item["sessions"]
        if not isinstance(rows, list) or not rows or "session" in item:
            raise ProtocolError("Baseline task requires one unambiguous session-reference list")
        return rows

    def _reference(self, reference: dict) -> Path:
        if set(reference) != {"path", "sha256"} or not re.fullmatch(r"[0-9a-f]{64}", str(reference["sha256"])):
            raise ProtocolError("Baseline references require path and sha256")
        relative = Path(reference["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ProtocolError("Baseline reference escapes its import bundle")
        path = self.directory / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(self.directory):
            raise ProtocolError("Baseline reference is missing, symlinked or outside the bundle")
        if hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
            raise ProtocolError("Baseline import reference digest changed")
        return path

    def verify_identity(self, expected: dict) -> None:
        if (self.manifest.get("schema") != BASELINE_SCHEMA
                or expected.get("identity_version") != "baseline-execution-v2"):
            raise ProtocolError("Legacy baseline identity is read-only; rebuild a verified v2 import for a new run")
        actual = self.manifest.get("execution_identity", {})
        selected = {field: expected.get(field) for field in self.IDENTITY_FIELDS}
        if actual != selected:
            raise ProtocolError("Baseline execution identity is incompatible; fresh fallback is forbidden")

    def sessions(self, tasks: Iterable[dict], expected_identity: dict) -> list[dict]:
        self.verify_identity(expected_identity)
        supplied = list(tasks)
        if len(supplied) != len(self.tasks) or {task["task_id"] for task in supplied} != set(self.tasks):
            raise ProtocolError("Baseline import task cohort differs")
        sessions = []
        for task in supplied:
            item = self.tasks[task["task_id"]]
            self._verify_task(item, task)
            refs = self._session_references(item)
            for index, reference in enumerate(refs):
                session = _read_json(self._reference(reference))
                outcome = session.get("outcome", {})
                expected_index = index + 1 if "sessions" in item else 0
                if (session.get("task_id") != task["task_id"] or not session.get("session_id")
                    or session.get("user_id") != task.get("user_id")
                    or session.get("phase") != "day"
                    or session.get("skill_bank_sha256") != expected_identity["s0_bank_sha256"]
                    or not session.get("original_session_id")
                    or not re.fullmatch(r"[0-9a-f]{64}", str(session.get("native_uploaded_sha256", "")))
                    or session.get("native_record_segment_index") != expected_index
                    or session.get("native_record_segment_count", len(refs)) != len(refs)
                    or outcome.get("score") != item.get("score")
                    or bool(outcome.get("timed_out")) != item.get("timed_out")):
                    raise ProtocolError("Imported Day session identity differs")
                sessions.append(session)
        if len({session["session_id"] for session in sessions}) != len(sessions):
            raise ProtocolError("Imported Day session identities are duplicated")
        return sessions

    def scored_row(self, task: dict, expected_identity: dict, bank_sha256: str) -> tuple[dict, dict]:
        self.verify_identity(expected_identity)
        if self.manifest["execution_identity"]["s0_bank_sha256"] != bank_sha256:
            raise ProtocolError("Imported Day baseline belongs to a different parent bank")
        item = self.tasks.get(task["task_id"])
        if item is None:
            raise ProtocolError("Imported Day baseline lacks a required task")
        self._verify_task(item, task)
        row = _read_json(self._reference(item["result"]))
        score = row.get("score")
        if (row.get("status") != "completed" or row.get("task_id") != task["task_id"]
                or isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 0 <= score <= 1):
            raise ProtocolError("Imported Day baseline is not a valid scored result")
        expected_score = item.get("score")
        if score != expected_score or bool(row.get("timed_out")) != item.get("timed_out"):
            raise ProtocolError("Imported Day score metadata differs")
        view = {**row, "short_id": task["short_id"], "user_id": task["user_id"],
                "category": task.get("category"), "skill_bank_sha256": bank_sha256}
        proof = {"policy": BASELINE_SCHEMA, "reused_without_execution_or_rescoring": True,
            "task_id": task["task_id"], "short_id": task["short_id"], "score": score,
            "timed_out": bool(row.get("timed_out")), "bank_sha256": bank_sha256,
            "source_result": item["result"], "source_sessions": self._session_references(item),
            "source_record_relabelled": False, "new_model_calls": 0}
        return view, proof

    @staticmethod
    def _verify_task(item: dict, task: dict) -> None:
        expected = {key: task.get(key) for key in ("task_id", "short_id", "user_id", "source_sha256", "input_sha256", "timeout_seconds")}
        actual = {key: item.get(key) for key in expected}
        if actual != expected:
            raise ProtocolError("Imported Day task/input identity differs")


def build_baseline_import_from_run(source: Path, destination: Path) -> BaselineImport:
    """Freeze one completed, current Day as a reusable baseline without model calls.

    Source files and their configuration stay untouched. Prompt/runtime digests
    come from the actual executions, and every finalized native segment survives.
    """
    from scripts.injection import e2e_30 as cohort
    from scripts.wildclawbench.protocol import tree_hash
    from scripts.wildclawbench import alignment_snapshot

    source, destination = source.resolve(), destination.absolute()
    settings = _read_json(source / "config.runtime.json")
    derived = _read_json(source / "derived-source.json")
    tasks = settings["all_tasks"]
    if not tasks or len({task["task_id"] for task in tasks}) != len(tasks):
        raise ProtocolError("Fresh baseline requires distinct tasks")
    cohort.validate_resume(source, settings, tree_hash,
                           expected_injected_ids=settings["injected_task_short_ids"])
    day = _read_json(source / "day/state.json", {})
    finalized = _read_json(source / "finalized-day-inputs/manifest.json", {})
    bank_hash = tree_hash(source / "banks/S0")
    if (day.get("status") != "completed" or day.get("scored_tasks") != len(tasks)
            or finalized.get("input_coverage_complete") is not True
            or finalized.get("task_count") != len(tasks) or finalized.get("bank_sha256") != bank_hash):
        raise ProtocolError("Fresh baseline lacks complete scored Day and finalized native inputs")
    prompts, contracts, rows = [], [], []
    variant_fields = ("approved_variant", "execution_preamble_source", "timeout_seconds", "judge_model",
                      "audio_judge_route", "shared_skill_bank", "search", "local_image_generation", "modified_checks_sha256")
    runtime_fields = ("benchmark_revision", "dataset_revision", "model_revision", "audio_model_revision",
                      "image_model_revision", "task_image_id", "external_judge", "judge_model_id", "judge_reasoning_effort")
    # EXECUTION_IDENTITY_FIELDS excludes the separate Night evolver timeout.
    comparable = EXECUTION_IDENTITY_FIELDS
    for task in tasks:
        loaded = cohort.load_completed_day(source, task)
        if loaded is None:
            raise ProtocolError("Fresh baseline contains an incomplete task")
        state, _ = loaded
        output, _ = cohort.day_paths(source, task, state)
        result_path = output / "experiment-result.json"
        result = _read_json(result_path)
        runtime = _read_json(output / "runtime-config.json")
        binding = _read_json(output / "execution-binding.json")
        execution = _read_json(output / "execution.json")
        score = result.get("score")
        expected_protocol = derived["source_protocol_hash"] if derived else settings.get("protocol_hash")
        scoring_run_id = (derived["tasks"][task["short_id"]]["scoring_run_id"] if derived
                          else binding.get("run_id"))
        if (result.get("task_id") != task["task_id"] or result.get("status") != "completed"
                or isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 0 <= score <= 1
                or score != state.get("task_score") or state.get("bank_sha256") != bank_hash
                or binding.get("bank_sha256") != bank_hash
                or binding.get("protocol_hash") != expected_protocol
                or result.get("run_id") != scoring_run_id
                or runtime.get("user_id") != task["user_id"]
                or runtime.get("task", {}).get("source_path") != task["source_path"]
                or execution.get("task_id") != task["task_id"] or execution.get("scoring_status") != "completed"):
            raise ProtocolError("Fresh baseline execution identity or score differs")
        compared = tuple(field for field in comparable if not derived or field not in JUDGE_IDENTITY_FIELDS)
        if any(runtime.get(field) != settings.get(field) for field in compared):
            raise ProtocolError("Fresh baseline effective sampling/budget differs from its declared configuration")
        identity = alignment_snapshot.execution_identity(task["task_id"], binding["run_id"], binding, runtime)
        seal = alignment_snapshot.verify_agent_output(output, identity)
        if execution.get("output_snapshot_sha256") != seal["manifest_sha256"]:
            raise ProtocolError("Fresh baseline Agent output seal differs")
        prompt_path = output / "execution-prompt.txt"
        variant = _read_json(output / "task-variant.json")
        prompt_sha = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
        if variant.get("execution_prompt_sha256") != prompt_sha:
            raise ProtocolError("Fresh baseline execution prompt differs from its variant")
        prompts.append({"task_id": task["task_id"], "sha256": prompt_sha})
        contracts.append({"task_id": task["task_id"],
            "variant": {field: variant.get(field) for field in variant_fields},
            "runtime": {field: runtime.get(field) for field in runtime_fields}})
        segments = cohort.finalized_day_segments(source, task)
        if not segments or any(proof.get("input_coverage_complete", proof.get("native_finalization", {}).get(
                "input_coverage_complete")) is not True for _, proof, _ in segments):
            raise ProtocolError("Fresh baseline native-session coverage is incomplete")
        rows.append((task, result_path, result, segments))
    identity = baseline_execution_identity({**settings,
        "agent_prompt_sha256": _canonical(prompts), "runtime_contract_sha256": _canonical(contracts)}, tasks, bank_hash)
    source_ref = {"run_dir": str(source), "config_sha256": hashlib.sha256((source / "config.runtime.json").read_bytes()).hexdigest(),
                  "finalized_inputs_sha256": hashlib.sha256((source / "finalized-day-inputs/manifest.json").read_bytes()).hexdigest()}
    if derived:
        source_ref["derived_source_sha256"] = hashlib.sha256((source / "derived-source.json").read_bytes()).hexdigest()
    if destination.exists():
        existing = BaselineImport(destination)
        existing.verify_identity(identity)
        if existing.manifest.get("source") != source_ref:
            raise ProtocolError("Existing fresh baseline import belongs to another Day")
        existing.sessions(tasks, identity)
        for task in tasks:
            existing.scored_row(task, identity, bank_hash)
        return existing
    building = destination.with_name(destination.name + f".building-{os.getpid()}")
    if building.exists():
        raise ProtocolError("Uncommitted baseline import exists; reconcile it before retry")
    building.mkdir(parents=True)
    records = []
    try:
        def copied(original: Path, relative: Path) -> dict:
            target = building / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            return {"path": relative.as_posix(), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}

        for task, result_path, result, segments in rows:
            short = task["short_id"]
            result_ref = copied(result_path, Path("results") / (short + ".json"))
            session_refs = []
            for _, proof, _ in segments:
                relative = Path(proof["selected_segment"]["session_path"])
                original = source / "finalized-day-inputs" / short / relative
                if relative.is_absolute() or ".." in relative.parts or not original.resolve().is_relative_to(source):
                    raise ProtocolError("Finalized session path escapes its source Day")
                session_refs.append(copied(original, Path("sessions") / short / relative))
            records.append({**{key: task.get(key) for key in (
                "task_id", "short_id", "user_id", "source_sha256", "input_sha256", "timeout_seconds")},
                "score": result["score"], "timed_out": bool(result.get("timed_out")),
                "result": result_ref, "sessions": session_refs})
        _write_json(building / "manifest.json", {"schema": BASELINE_SCHEMA, "task_count": len(tasks),
            "execution_identity": identity, "tasks": records, "source": source_ref})
        candidate = BaselineImport(building)
        candidate.sessions(tasks, identity)
        for task in tasks:
            candidate.scored_row(task, identity, bank_hash)
        os.replace(building, destination)
    except BaseException:
        shutil.rmtree(building)
        raise
    return BaselineImport(destination)


def _file_reference(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ProtocolError(f"Missing or symlinked evidence: {path}")
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _verify_file_reference(reference: dict) -> Path:
    path = Path(reference["path"])
    if _file_reference(path) != reference:
        raise ProtocolError(f"Preserved evidence changed: {path}")
    return path


def _verify_source_changes(before: list[dict], after: list[dict], reviews: list[dict]) -> None:
    old, new = ({item["path"]: item["sha256"] for item in rows} for rows in (before, after))
    changed = {path: (old.get(path), new.get(path)) for path in old.keys() | new.keys()
               if old.get(path) != new.get(path)}
    supplied = {item["path"]: item for item in reviews}
    if len(supplied) != len(reviews) or set(supplied) != set(changed):
        raise ProtocolError("Source compatibility review must cover exactly every changed source")
    scopes = {"grading_only", "resume_only", "night_validation", "experiment_definition", "backend_only"}
    for path, pair in changed.items():
        item = supplied[path]
        if ((item.get("before_sha256"), item.get("after_sha256")) != pair
                or item.get("scope") not in scopes or not str(item.get("reason", "")).strip()):
            raise ProtocolError("Source compatibility review has an unverified change: " + path)


def derived_source_files(source: Path, tasks: list[dict]) -> tuple[list[dict], list[dict]]:
    """List the preserved/current inputs to an explicit compatibility review."""
    from scripts.injection import e2e, e2e_30 as cohort
    before = cohort.verify_frozen_sources(source, require_live=False)
    current = {item["path"]: _file_reference(Path(item["path"])) for item in before}
    for task in tasks:
        for item in e2e.source_manifest(e2e.ROOT / "scripts/wildclawbench", task)["files"]:
            current[item["path"]] = item
    return before, [current[path] for path in sorted(current)]


def _check_reused_agent(source_output: Path, task: dict, source_settings: dict, settings: dict) -> dict:
    """Authenticate original Agent evidence and render the new task prompt offline."""
    from scripts.wildclawbench import alignment_snapshot, task_runtime
    execution = _read_json(source_output / "execution.json")
    runtime = _read_json(source_output / "runtime-config.json")
    binding = _read_json(source_output / "execution-binding.json")
    if execution.get("status") != "agent_completed" or execution.get("task_id") != task["task_id"]:
        raise ProtocolError("Only a completed original Agent execution can be reused")
    if any(runtime.get(field) != source_settings.get(field) for field in AGENT_REUSE_FIELDS):
        raise ProtocolError("Actual original Agent parameters differ from their declared source")
    if any(settings.get(field) != source_settings.get(field) for field in AGENT_REUSE_FIELDS):
        raise ProtocolError("Changed Agent sampling, input runtime or task policy requires a new execution")
    identity = alignment_snapshot.execution_identity(task["task_id"], binding["run_id"], binding, runtime)
    seal = alignment_snapshot.verify_agent_output(source_output, identity)
    if execution.get("output_snapshot_sha256") != seal["manifest_sha256"]:
        raise ProtocolError("Original Agent execution references a different output seal")
    config = {**settings, "task": task, "user_id": task["user_id"]}
    spec = task_runtime.task_spec(task["task_id"], config)
    spec["timeout_seconds"] = min(int(spec["timeout_seconds"]), int(config["task_timeout_seconds"]))
    with tempfile.TemporaryDirectory(prefix="sharedskill-agent-prompt-") as directory:
        rendered = task_runtime._variants(spec, config, Path(directory), "http://127.0.0.1/unused/v1")
        expected = (source_output / "execution-prompt.txt").read_bytes()
        if rendered["prompt"].encode() != expected:
            raise ProtocolError("Current task rendering changes the original Agent prompt")
    return {"agent_manifest_sha256": seal["manifest_sha256"],
            "agent_prompt_sha256": hashlib.sha256(expected).hexdigest(), "source_run_id": binding["run_id"]}


def validate_derived_source(run: Path, settings: dict, tree_hash, *, require_live: bool = True) -> None:
    """A derived source retains old Agent identity and independently proven scores."""
    from scripts.injection import e2e_30 as cohort
    from scripts.wildclawbench import grade_recovery, alignment_snapshot
    proof = _read_json(run / "derived-source.json", {})
    if proof.get("schema") != DERIVED_SOURCE_SCHEMA or proof.get("status") != "completed":
        raise ProtocolError("Derived baseline source is not committed")
    if _file_reference(run / "config.runtime.json") != proof["configuration"] or _read_json(run / "config.runtime.json") != settings:
        raise ProtocolError("Derived source configuration changed")
    source = Path(proof["source_run"])
    parent_settings = _read_json(_verify_file_reference(proof["source_configuration"]))
    if (source / "derived-source.json").exists():
        raise ProtocolError("Chained derived sources require a separately reviewed provenance policy")
    cohort.validate_resume(source, parent_settings, tree_hash,
                           expected_injected_ids=parent_settings["injected_task_short_ids"], frozen_source_only=True)
    before = cohort.verify_frozen_sources(source, require_live=False)
    after = cohort.verify_frozen_sources(run, require_live=require_live)
    _verify_source_changes(before, after, proof["source_changes"])
    if (settings["all_tasks"] != parent_settings["all_tasks"]
            or any(settings.get(field) != parent_settings.get(field) for field in AGENT_REUSE_FIELDS)
            or tree_hash(run / "banks/S0") != proof["bank_sha256"]
            or tree_hash(source / "banks/S0") != proof["bank_sha256"]):
        raise ProtocolError("Derived source changed the original task, Agent policy or S0")
    for task in settings["all_tasks"]:
        item = proof["tasks"][task["short_id"]]
        receipt_path = _verify_file_reference(item["grading_receipt"])
        receipt = grade_recovery.verify_recovery(receipt_path.parent)
        if receipt.get("status") != "completed" or Path(receipt["source_output"]).resolve() != Path(item["source_output"]):
            raise ProtocolError("Independent grading is incomplete or belongs to another Agent")
        for reference in item["source_evidence"].values():
            _verify_file_reference(reference)
        _verify_file_reference(item["original_session"])
        output = run / "day/tasks" / task["short_id"] / "task"
        binding, runtime = (_read_json(output / name) for name in ("execution-binding.json", "runtime-config.json"))
        identity = alignment_snapshot.execution_identity(task["task_id"], binding["run_id"], binding, runtime)
        seal = alignment_snapshot.verify_agent_output(output, identity)
        if seal["manifest_sha256"] != item["agent_manifest_sha256"]:
            raise ProtocolError("Derived source changed the completed Agent snapshot")
        result = _read_json(output / "experiment-result.json")
        if _file_reference(output / "experiment-result.json") != item["result"]:
            raise ProtocolError("Derived source score differs from its grading import")
        if result != _read_json(_verify_file_reference(item["grading_result"])):
            raise ProtocolError("Derived source score differs from the independent grading attempt")
        loaded = cohort.load_completed_day(run, task)
        if loaded is None or loaded[0]["task_score"] != result["score"]:
            raise ProtocolError("Derived source contains an incomplete scored task")
    from scripts.injection.cycle_validation import _bound_tasks, validate_cohort_inputs
    bound = _bound_tasks(settings["cohort_validation_binding"])
    for task in settings["all_tasks"]:
        validate_cohort_inputs(settings["cohort_validation_binding"], task, expected_tasks=bound)
    finalized = _read_json(run / "finalized-day-inputs/manifest.json", {})
    if (finalized.get("input_coverage_complete") is not True
            or finalized.get("task_count") != len(settings["all_tasks"])):
        raise ProtocolError("Derived source lacks complete native trajectory coverage")
    cohort.finalize_day_sessions(run, settings["all_tasks"], [], proof["bank_sha256"])


def build_derived_baseline_source(source: Path, destination: Path, attempts: dict[str, Path],
                                  requested: dict, source_changes: list[dict]) -> Path:
    """Create a new source ID after reviewed code repairs, without executing Agents.

    The caller supplies an explicit review of every changed source digest. This
    records a reviewed compatibility claim, not a claim of identical backends.
    """
    from scripts.injection import e2e, e2e_30 as cohort
    from scripts.injection.cycle_validation import prepare_input_binding
    from scripts.wildclawbench import grade_recovery
    from scripts.wildclawbench.protocol import tree_hash
    source, destination = source.resolve(), destination.resolve()
    if destination.exists():
        raise ProtocolError("Derived source ID already exists; verify it instead of rebuilding")
    original = _read_json(source / "config.runtime.json")
    if (source / "derived-source.json").exists():
        raise ProtocolError("Only original Agent runs can be the parent of a derived source")
    cohort.validate_resume(source, original, tree_hash,
                           expected_injected_ids=original["injected_task_short_ids"], frozen_source_only=True)
    tasks = original["all_tasks"]
    if set(attempts) != {task["short_id"] for task in tasks}:
        raise ProtocolError("Independent grading must cover the complete frozen task cohort")
    settings = {**original, **requested, "all_tasks": tasks, "task": original["task"],
                "task_manifest_sha256": original["task_manifest_sha256"]}
    cohort.validate_local_execution(settings)
    before, after = derived_source_files(source, tasks)
    _verify_source_changes(before, after, source_changes)
    records = {}
    for task in tasks:
        short = task["short_id"]
        prior_state = _read_json(source / "day/tasks" / short / "state.json")
        output, service = cohort.day_paths(source, task, prior_state)
        agent = _check_reused_agent(output, task, original, settings)
        attempt = Path(attempts[short]).resolve()
        receipt = grade_recovery.verify_recovery(attempt)
        result_path = attempt / receipt["result_path"]
        result = _read_json(result_path)
        score = result.get("score")
        if (receipt.get("status") != "completed" or Path(receipt["source_output"]).resolve() != output.resolve()
                or result.get("task_id") != task["task_id"] or result.get("status") != "completed"
                or isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 0 <= score <= 1):
            raise ProtocolError("Derived source needs a verified finite score for every original Agent")
        attempt_config = _read_json(attempt / "runtime-config.json")
        if any(attempt_config.get(key) != settings.get(key) for key in JUDGE_IDENTITY_FIELDS):
            raise ProtocolError("Independent grading policy differs from the derived comparison")
        records[short] = {**agent, "source_output": str(output.resolve()), "source_service": str(service.resolve()),
            "grading_attempt": str(attempt), "grading_receipt": _file_reference(attempt / "grade-recovery.json"),
            "grading_result": _file_reference(result_path), "scoring_run_id": result.get("run_id"),
            "source_evidence": {name: _file_reference(output / name) for name in (
                "execution.json", "execution-binding.json", "runtime-config.json", "agent-output-manifest.json")}}
        upload = _read_json(service / "upload-evidence.json")
        for relative in ("uploaded-session.json", "upload-evidence.json", "service.json", "records/conversations.jsonl",
                         "shared-objects/" + upload["object_key"]):
            records[short]["source_evidence"]["native:" + relative] = _file_reference(service / relative)
    destination.mkdir(parents=True)
    try:
        shutil.copytree(source / "banks/S0", destination / "banks/S0")
        shutil.copytree(source / "frozen/tasks", destination / "frozen/tasks")
        shutil.copyfile(source / "phase-handoff.json", destination / "phase-handoff.json")
        sessions = []
        for task in tasks:
            short, item = task["short_id"], records[task["short_id"]]
            output = destination / "day/tasks" / short / "task"
            shutil.copytree(item["grading_attempt"], output)
            result = _read_json(_verify_file_reference(item["grading_result"]))
            _write_json(output / "experiment-result.json", result)
            item["result"] = _file_reference(output / "experiment-result.json")
            service = destination / "services" / ("day-" + short)
            shutil.copytree(item["source_service"], service)
            state = _read_json(source / "day/tasks" / short / "state.json")
            session = _read_json(service / "native-day-session.json")
            item["original_session"] = _file_reference(Path(item["source_service"]) / "native-day-session.json")
            session["outcome"] = {"score": result["score"], "timed_out": bool(result.get("timed_out")),
                                  "tool_errors": int(result.get("tool_errors") or 0)}
            _write_json(service / "native-day-session.json", session)
            sessions.append(session)
            _write_json(destination / "day/tasks" / short / "state.json", {**state,
                "status": "completed", "task_status": "completed", "task_score": result["score"],
                "timed_out": bool(result.get("timed_out")), "task_output": str(output.relative_to(destination)),
                "service_dir": str(service.relative_to(destination)), "grade_recovery": item["grading_receipt"]})
        settings.update(runtime_dir=str(destination / "runtime"),
                        gateway_log_dir=str(e2e.new_gateway_evidence_dir(destination, settings)))
        settings.pop("stage_budget", None)
        settings["cohort_validation_binding"] = prepare_input_binding(destination, tasks)
        settings["protocol_hash"] = _canonical({"source_protocol": original["protocol_hash"],
            "sources": after, "source_changes": source_changes, "grading": records,
            "settings": {key: value for key, value in settings.items() if key != "protocol_hash"}})
        _write_json(destination / "config.runtime.json", settings)
        _write_json(destination / "state.json", {"status": "baseline_ready", "run_id": destination.name,
            "injected_task_short_ids": settings["injected_task_short_ids"], "new_agent_executions": 0})
        _write_json(destination / "day/state.json", {"status": "completed", "scored_tasks": len(tasks),
            "task_count": len(tasks), "uploaded_sessions": len(tasks),
            "mean_score": sum(session["outcome"]["score"] for session in sessions) / len(tasks)})
        _write_json(destination / "day/task/shared-skill-names.json", sorted(
            path.parent.name for path in (destination / "banks/S0").glob("*/SKILL.md")))
        for index, item in enumerate(after):
            path = destination / "frozen/source" / f"{index:04d}-{Path(item['path']).name}"
            path.parent.mkdir(parents=True, exist_ok=True)
            data = _verify_file_reference(item).read_bytes()
            path.write_bytes(data)
        _write_json(destination / "frozen-source.json", {"files": after, "sha256": _canonical(after)})
        cohort.finalize_day_sessions(destination, tasks, sessions, tree_hash(destination / "banks/S0"))
        proof = {"schema": DERIVED_SOURCE_SCHEMA, "status": "completed", "source_run": str(source),
            "source_configuration": _file_reference(source / "config.runtime.json"),
            "configuration": _file_reference(destination / "config.runtime.json"),
            "source_protocol_hash": original["protocol_hash"], "source_changes": source_changes,
            "bank_sha256": tree_hash(source / "banks/S0"), "tasks": records,
            "agent_execution": "original unchanged sealed outputs; no Agent rerun",
            "session_outcome": "derived from independent scoring; original native uploads unchanged",
            "backend_differences": {key: {"source": original.get(key), "current": settings.get(key)} for key in (
                "language_model_only", "gpu_memory_utilization", "limit_mm_per_prompt", "mm_processor_kwargs")
                if original.get(key) != settings.get(key)},
            "limitation": "Reviewed source compatibility and prompt equality do not prove identical inference backends."}
        _write_json(destination / "derived-source.json", proof)
        validate_derived_source(destination, settings, tree_hash)
    except BaseException:
        _write_json(destination / "state.json", {"status": "derived_source_incomplete", "new_agent_executions": 0})
        raise
    return destination


def build_baseline_import_from_archive(
    repository: Path, archive_run_id: str, source_stage_reference: str,
    destination: Path, tasks: Iterable[dict], expected_identity: dict,
) -> BaselineImport:
    """Create the minimal verified Day1 bundle from a migrated archive.

    ``source_stage_reference`` is the original absolute run path recorded by
    the frozen evidence. Every source file is located through ``Archive``;
    callers never depend on the artifact/output mirror layout.
    """
    from scripts.archive import Archive, ArchiveError, digest as file_digest

    repository, destination = repository.resolve(), destination.absolute()
    if not ARCHIVE_RUN_ID.fullmatch(archive_run_id):
        raise ProtocolError("Unsafe archive run id")
    source = Path(source_stage_reference)
    if not source.is_absolute() or ".." in source.parts:
        raise ProtocolError("source_stage_reference must be a normalized absolute path")
    supplied = list(tasks)
    if len(supplied) != 30 or len({task.get("task_id") for task in supplied}) != 30:
        raise ProtocolError("Baseline import preflight requires exactly 30 distinct tasks")
    selected_identity = {field: expected_identity.get(field) for field in BaselineImport.IDENTITY_FIELDS}
    if baseline_execution_identity(selected_identity, supplied, selected_identity.get("s0_bank_sha256")) != selected_identity:
        raise ProtocolError("Expected baseline execution identity is incomplete")

    archive = Archive(repository / "artifacts" / archive_run_id / "archive.json", repository)
    try:
        audit_path, _ = archive.read_reference(archive.data["audit"])
        audit = json.loads(audit_path.read_text())
        evidence = audit["proof"]["evidence"]
    except (ArchiveError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProtocolError(f"Archive audit cannot be authenticated: {error}") from error
    audited: dict[str, dict[str, str]] = {}
    for reference in evidence:
        if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"}
                or not isinstance(reference["path"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(reference["sha256"]))):
            raise ProtocolError("Archive audit contains an invalid evidence reference")
        prior = audited.setdefault(reference["path"], reference)
        if prior["sha256"] != reference["sha256"]:
            raise ProtocolError(f"Archive audit has conflicting digests: {reference['path']}")

    def resolve(relative: str) -> Path:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ProtocolError(f"Archived Day reference escapes its source stage: {relative}")
        original = str(source / relative_path)
        reference = audited.get(original)
        if reference is None:
            raise ProtocolError(f"Archived Day reference is absent from the authenticated audit: {relative}")
        try:
            path, _ = archive.read_reference(reference)
        except (ArchiveError, OSError) as error:
            raise ProtocolError(f"Archived Day reference failed digest verification: {relative}") from error
        return path

    def read(relative: str) -> dict:
        path = resolve(relative)
        value = _read_json(path)
        if not isinstance(value, dict):
            raise ProtocolError(f"Archived JSON object is invalid: {relative}")
        return value

    source_config_path = resolve("config.runtime.json")
    source_config = _read_json(source_config_path)
    source_day_path = resolve("day/state.json")
    source_day = _read_json(source_day_path)
    if (source_day.get("status") != "completed" or source_day.get("scored_tasks") != 30
            or len(source_config.get("all_tasks", [])) != 30):
        raise ProtocolError("Archived Day1 is not a complete 30-task scored stage")
    source_tasks = {task["task_id"]: task for task in source_config["all_tasks"]}
    comparable = EXECUTION_IDENTITY_FIELDS
    if any(source_config.get(field) != selected_identity.get(field) for field in comparable):
        raise ProtocolError("Archived Day1 model or sampling identity differs")

    if destination.exists():
        imported = BaselineImport(destination)
        imported.verify_identity(selected_identity)
        if imported.manifest.get("source", {}).get("archive_run_id") != archive_run_id:
            raise ProtocolError("Existing baseline bundle belongs to another archive")
        imported.sessions(supplied, selected_identity)
        for task in supplied:
            imported.scored_row(task, selected_identity, selected_identity["s0_bank_sha256"])
        return imported

    building = destination.with_name(destination.name + f".building-{os.getpid()}")
    if building.exists():
        shutil.rmtree(building)
    building.mkdir(parents=True)
    records, prompts, contracts = [], [], []
    try:
        for task in supplied:
            archived_task = source_tasks.get(task["task_id"])
            expected_task = {key: task.get(key) for key in (
                "task_id", "short_id", "user_id", "source_sha256", "input_sha256", "timeout_seconds")}
            actual_task = {key: archived_task.get(key) if archived_task else None for key in expected_task}
            if actual_task != expected_task:
                raise ProtocolError(f"Archived task/input identity differs: {task.get('short_id')}")
            short = task["short_id"]
            state_relative = f"day/tasks/{short}/state.json"
            state_path = resolve(state_relative)
            state = _read_json(state_path)
            if (state.get("status") != "completed" or state.get("task_id") != task["task_id"]
                    or state.get("short_id") != short or state.get("user_id") != task.get("user_id")
                    or state.get("bank_sha256") != selected_identity["s0_bank_sha256"]):
                raise ProtocolError(f"Archived Day task state is incompatible: {short}")
            task_output = state.get("task_output", f"day/tasks/{short}/task")
            service_dir = state.get("service_dir", f"services/day-{short}")
            for field, value in (("task_output", task_output), ("service_dir", service_dir)):
                path = Path(str(value))
                if path.is_absolute() or ".." in path.parts:
                    raise ProtocolError(f"Archived {field} escapes its Day stage: {short}")
            result_path = resolve(f"{task_output}/experiment-result.json")
            session_path = resolve(f"{service_dir}/native-day-session.json")
            prompt_path = resolve(f"{task_output}/execution-prompt.txt")
            variant_path = resolve(f"{task_output}/task-variant.json")
            runtime_path = resolve(f"{task_output}/runtime-config.json")
            upload_path = resolve(f"{service_dir}/upload-evidence.json")
            result, session = _read_json(result_path), _read_json(session_path)
            variant, runtime, upload = _read_json(variant_path), _read_json(runtime_path), _read_json(upload_path)
            score = result.get("score")
            if (result.get("status") != "completed" or result.get("task_id") != task["task_id"]
                    or isinstance(score, bool) or not isinstance(score, (int, float))
                    or not math.isfinite(score) or not 0 <= score <= 1
                    or score != state.get("task_score")
                    or bool(result.get("timed_out")) != bool(state.get("timed_out"))):
                raise ProtocolError(f"Archived Day result is invalid: {short}")
            uploaded_sha = state.get("uploaded_sha256")
            if (not re.fullmatch(r"[0-9a-f]{64}", str(uploaded_sha or ""))
                    or upload.get("uploaded_sha256") != uploaded_sha
                    or upload.get("session_id") != session.get("session_id")):
                raise ProtocolError(f"Archived native upload identity differs: {short}")
            normalized_session = {**session,
                "original_session_id": session["session_id"],
                "native_uploaded_sha256": uploaded_sha,
                "native_record_segment_index": 0,
                "native_record_segment_count": 1}
            if (normalized_session.get("task_id") != task["task_id"]
                    or normalized_session.get("skill_bank_sha256") != selected_identity["s0_bank_sha256"]
                    or normalized_session.get("outcome", {}).get("score") != score):
                raise ProtocolError(f"Archived native Day session differs: {short}")
            result_target = building / "results" / f"{short}.json"
            session_target = building / "sessions" / f"{short}.json"
            result_target.parent.mkdir(parents=True, exist_ok=True)
            session_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(result_path, result_target)
            _write_json(session_target, normalized_session)
            prompts.append({"task_id": task["task_id"], "sha256": file_digest(prompt_path)})
            contract_fields = ("approved_variant", "execution_preamble_source", "timeout_seconds",
                               "judge_model", "audio_judge_route", "shared_skill_bank", "search",
                               "local_image_generation", "modified_checks_sha256")
            runtime_fields = ("benchmark_revision", "dataset_revision", "model_revision",
                              "audio_model_revision", "image_model_revision", "task_image_id",
                              "external_judge", "judge_model_id", "judge_reasoning_effort")
            contracts.append({"task_id": task["task_id"],
                "variant": {field: variant.get(field) for field in contract_fields},
                "runtime": {field: runtime.get(field) for field in runtime_fields}})
            records.append({**expected_task, "score": score,
                "timed_out": bool(result.get("timed_out")),
                "result": {"path": result_target.relative_to(building).as_posix(),
                           "sha256": file_digest(result_target)},
                "session": {"path": session_target.relative_to(building).as_posix(),
                            "sha256": file_digest(session_target)}})
        if _canonical(prompts) != selected_identity["agent_prompt_sha256"]:
            raise ProtocolError("Archived Agent prompt digest differs from the planned Day1 identity")
        if _canonical(contracts) != selected_identity["runtime_contract_sha256"]:
            raise ProtocolError("Archived runtime contract digest differs from the planned Day1 identity")
        _write_json(building / "manifest.json", {"schema": BASELINE_SCHEMA, "task_count": 30,
            "execution_identity": selected_identity, "tasks": records,
            "source": {"archive_run_id": archive_run_id,
                "source_stage_reference": str(source),
                "config_sha256": file_digest(source_config_path),
                "day_state_sha256": file_digest(source_day_path)}})
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(building, destination)
    except Exception:
        shutil.rmtree(building, ignore_errors=True)
        raise
    imported = BaselineImport(destination)
    imported.sessions(supplied, selected_identity)
    for task in supplied:
        imported.scored_row(task, selected_identity, selected_identity["s0_bank_sha256"])
    return imported


def _main() -> int:
    parser = argparse.ArgumentParser(description="Build a digest-bound 30-task Day1 import bundle")
    parser.add_argument("--archive-run-id", required=True)
    parser.add_argument("--source-stage-reference", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--current-runtime-config", type=Path, required=True,
                        help="Prepared config.runtime.json containing all_tasks and execution identity fields")
    args = parser.parse_args()
    config = _read_json(args.current_runtime_config)
    tasks = config.get("all_tasks", [])
    identity = baseline_execution_identity(config, tasks, config.get("s0_bank_sha256"))
    bundle = build_baseline_import_from_archive(
        Path(__file__).resolve().parents[2], args.archive_run_id,
        args.source_stage_reference, args.destination, tasks, identity)
    print(json.dumps({"bundle": str(bundle.directory), "tasks": len(bundle.tasks),
                      "manifest_sha256": hashlib.sha256(bundle.manifest_path.read_bytes()).hexdigest()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
