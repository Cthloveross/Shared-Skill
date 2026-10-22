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
import threading
import time
from typing import Any, Callable, Iterable, Literal


VERSION = "resilient-v1"
QUEUE_SCHEMA = "resilient-case-queue-v1"
BASELINE_SCHEMA = "same-stage-day-baseline-import-v1"
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
        "task_id", "short_id", "user_id", "source_sha256", "input_sha256"
    )} for task in tasks]
    identity = {
        "task_manifest_sha256": config.get("task_manifest_sha256", _canonical(task_rows)),
        "s0_bank_sha256": bank_sha256,
        "model_id": config.get("model_id"), "model_revision": config.get("model_revision"),
        "temperature": config.get("temperature"), "reasoning_effort": config.get("reasoning_effort"),
        "max_tokens": config.get("max_tokens"), "context_length": config.get("context_length"),
        "agent_prompt_sha256": config.get("agent_prompt_sha256"),
        "runtime_contract_sha256": config.get("runtime_contract_sha256"), "seed": config.get("seed"),
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
    fields = ("protocol_hash", "model_id", "model_revision", "seed", "temperature", "reasoning_effort",
              "max_tokens", "context_length", "task_timeout_seconds", "grading_timeout_seconds",
              "external_judge", "judge_model_id", "agent_prompt_sha256", "runtime_contract_sha256")
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
    """Read a preflight-built, digest-bound 30-task Day import bundle."""

    IDENTITY_FIELDS = (
        "task_manifest_sha256", "s0_bank_sha256", "model_id", "model_revision",
        "temperature", "reasoning_effort", "max_tokens", "context_length",
        "agent_prompt_sha256", "runtime_contract_sha256", "seed",
    )

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        if directory.is_symlink() or not self.directory.is_dir():
            raise ProtocolError("baseline_import bundle is missing or symlinked")
        self.manifest_path = self.directory / "manifest.json"
        if self.manifest_path.is_symlink() or not self.manifest_path.is_file():
            raise ProtocolError("baseline_import manifest is missing or symlinked")
        self.manifest = _read_json(self.manifest_path)
        if not isinstance(self.manifest, dict) or self.manifest.get("schema") != BASELINE_SCHEMA:
            raise ProtocolError("Unknown baseline import bundle schema")
        tasks = self.manifest.get("tasks")
        if self.manifest.get("task_count") != 30 or not isinstance(tasks, list) or len(tasks) != 30:
            raise ProtocolError("Baseline import must contain exactly 30 tasks")
        self.tasks = {item.get("task_id"): item for item in tasks}
        if None in self.tasks or len(self.tasks) != 30:
            raise ProtocolError("Baseline import task identities are missing or duplicated")
        for item in tasks:
            for field in ("result", "session"):
                reference = item.get(field)
                if not isinstance(reference, dict):
                    raise ProtocolError(f"Baseline task lacks {field} reference")
                self._reference(reference)

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
        actual = self.manifest.get("execution_identity", {})
        selected = {field: expected.get(field) for field in self.IDENTITY_FIELDS}
        if actual != selected:
            raise ProtocolError("Baseline execution identity is incompatible; fresh fallback is forbidden")

    def sessions(self, tasks: Iterable[dict], expected_identity: dict) -> list[dict]:
        self.verify_identity(expected_identity)
        supplied = list(tasks)
        if {task["task_id"] for task in supplied} != set(self.tasks):
            raise ProtocolError("Baseline import task cohort differs")
        sessions = []
        for task in supplied:
            item = self.tasks[task["task_id"]]
            self._verify_task(item, task)
            session = _read_json(self._reference(item["session"]))
            outcome = session.get("outcome", {})
            if (session.get("task_id") != task["task_id"] or not session.get("session_id")
                    or session.get("user_id") != task.get("user_id")
                    or session.get("phase") != "day"
                    or session.get("skill_bank_sha256") != expected_identity["s0_bank_sha256"]
                    or not session.get("original_session_id")
                    or not re.fullmatch(r"[0-9a-f]{64}", str(session.get("native_uploaded_sha256", "")))
                    or session.get("native_record_segment_index") != 0
                    or session.get("native_record_segment_count") != 1
                    or outcome.get("score") != item.get("score")
                    or bool(outcome.get("timed_out")) != item.get("timed_out")):
                raise ProtocolError("Imported Day session identity differs")
            sessions.append(session)
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
            "source_result": item["result"], "source_session": item["session"],
            "source_record_relabelled": False, "new_model_calls": 0}
        return view, proof

    @staticmethod
    def _verify_task(item: dict, task: dict) -> None:
        expected = {key: task.get(key) for key in ("task_id", "short_id", "user_id", "source_sha256", "input_sha256")}
        actual = {key: item.get(key) for key in expected}
        if actual != expected:
            raise ProtocolError("Imported Day task/input identity differs")


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
    comparable = ("model_id", "model_revision", "temperature", "reasoning_effort",
                  "max_tokens", "context_length", "seed")
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
                "task_id", "short_id", "user_id", "source_sha256", "input_sha256")}
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
