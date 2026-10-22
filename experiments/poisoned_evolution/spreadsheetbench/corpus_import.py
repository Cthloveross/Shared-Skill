"""Offline, provenance-preserving reuse of completed corpus cases.

Neither entry point invokes a model or a case executor. The historical import
copies evidence and re-scores existing workbooks. ``derive_completed_corpus``
instead preserves scoring and references the frozen files without copying them.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .corpus import (
    CorpusConfig,
    _apply_scoring_rule,
    _carrier_eligible,
    adapt_session_for_evolution,
    build_session_payload,
    case_record_to_session,
)
from .layout import resolve_path
from .runtime import ScoreResult, SpreadsheetCase, SpreadsheetTask, load_tasks, score_workbook

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_SOURCE = Path(__file__).with_name("runtime.py")


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve(path: str | Path) -> Path:
    return resolve_path(path, repo_root=REPO_ROOT)


def _run_roots(config: Mapping[str, Any]) -> tuple[str, Path, Path]:
    run_ids = config.get("run_ids")
    paths = config.get("paths")
    if not isinstance(run_ids, Mapping) or not isinstance(paths, Mapping):
        raise ValueError("config is missing run_ids or paths")
    run_id = str(run_ids.get("corpus") or "")
    if not run_id:
        raise ValueError("config is missing the corpus run id")
    artifacts = _resolve(str(paths.get("corpus_artifacts") or Path("artifacts") / run_id))
    outputs = _resolve(str(paths.get("corpus_outputs") or Path("outputs") / run_id))
    if artifacts.name != run_id or outputs.name != run_id:
        raise ValueError("corpus artifact/output roots must end in the corpus run id")
    return run_id, artifacts, outputs


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "item"


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def _stable_identity(config: Mapping[str, Any]) -> dict[str, Any]:
    sources = config.get("sources") if isinstance(config.get("sources"), Mapping) else {}
    model = config.get("model") if isinstance(config.get("model"), Mapping) else {}
    corpus = config.get("corpus") if isinstance(config.get("corpus"), Mapping) else {}
    executor = corpus.get("executor") if isinstance(corpus.get("executor"), Mapping) else {}
    return {
        "sources": {
            key: sources.get(key)
            for key in (
                "dataset_sha256",
                "subset_tree_sha256",
                "selection_sha256",
                "evaluator_sha256",
                "source_archive_sha256",
                "xlsx_bundle_sha256",
            )
        },
        "model": {
            key: model.get(key)
            for key in (
                "id",
                "revision",
                "temperature",
                "top_p",
                "max_tokens",
                "context_length",
                "chat_template_kwargs",
            )
        },
        "corpus": {
            "task_count": corpus.get("task_count"),
            "cases_per_task": corpus.get("cases_per_task"),
            "generate_once_replay_cases": corpus.get("generate_once_replay_cases"),
            "executor": dict(executor),
        },
    }


class _Plan:
    def __init__(self) -> None:
        self.files: dict[Path, tuple[Path, bytes, str]] = {}

    def add(self, source: Path, destination: Path, *, kind: str, payload: bytes | None = None) -> None:
        if not source.is_file():
            raise FileNotFoundError(source)
        content = source.read_bytes() if payload is None else payload
        prior = self.files.get(destination)
        if prior is not None:
            if prior[1] != content:
                raise RuntimeError(f"two import inputs disagree for {destination}")
            return
        self.files[destination] = (source, content, kind)

    def add_tree(self, source: Path, destination: Path, *, kind: str) -> None:
        if not source.is_dir():
            raise FileNotFoundError(source)
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            if path.is_symlink():
                raise ValueError(f"symlinks are not accepted as corpus evidence: {path}")
            self.add(path, destination / path.relative_to(source), kind=kind)

    def commit(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for destination in sorted(self.files, key=lambda item: _relative(item)):
            source, payload, kind = self.files[destination]
            source_digest = _sha256_file(source)
            destination_digest = _sha256_bytes(payload)
            if destination.exists():
                if not destination.is_file() or _sha256_file(destination) != destination_digest:
                    raise RuntimeError(f"refusing to overwrite differing destination evidence: {destination}")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
                try:
                    temporary.write_bytes(payload)
                    if _sha256_file(temporary) != destination_digest:
                        raise RuntimeError(f"staged evidence digest mismatch: {destination}")
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            observed = _sha256_file(destination)
            if observed != destination_digest:
                raise RuntimeError(f"destination evidence digest mismatch: {destination}")
            records.append(
                {
                    "kind": kind,
                    "source": _relative(source),
                    "destination": _relative(destination),
                    "source_sha256": source_digest,
                    "destination_sha256": observed,
                    "byte_identical": source_digest == observed,
                }
            )
        return records


def _final_execution(model_record: dict[str, Any]) -> dict[str, Any]:
    steps = model_record.get("agent_steps")
    if not isinstance(steps, list):
        raise ValueError("generated model record has no agent_steps")
    for step in reversed(steps):
        if isinstance(step, dict) and isinstance(step.get("execution"), dict) and not step.get("parse_error"):
            return step["execution"]
    raise ValueError("generated model record has no final execution")


def _score_case(
    scorer: Callable[..., ScoreResult],
    *,
    task: SpreadsheetTask,
    case: SpreadsheetCase,
    output_path: Path,
    evaluator_path: Path,
) -> ScoreResult:
    result = scorer(task=task, case=case, output_path=output_path, evaluator_path=evaluator_path)
    if result.status != "completed" or result.passed is None or result.score not in {0.0, 1.0}:
        raise RuntimeError(f"destination scoring remained unknown for {case.case_id}: {result.message}")
    return result


def import_completed_corpus(
    source_config_path: str | Path,
    destination_config_path: str | Path,
    *,
    scorer: Callable[..., ScoreResult] = score_workbook,
) -> dict[str, Any]:
    """Import completed cases and re-score them without model/executor work."""

    source_config_path = _resolve(source_config_path)
    destination_config_path = _resolve(destination_config_path)
    source_config = _json(source_config_path)
    destination_config = _json(destination_config_path)
    source_run, source_artifacts, source_outputs = _run_roots(source_config)
    destination_run, destination_artifacts, destination_outputs = _run_roots(destination_config)
    if source_run == destination_run:
        raise ValueError("source and destination corpus run ids must differ")
    if _stable_identity(source_config) != _stable_identity(destination_config):
        raise ValueError("source and destination scientific acquisition identities differ")

    frozen_source = source_artifacts / "frozen-config.json"
    if not frozen_source.is_file() or frozen_source.read_bytes() != source_config_path.read_bytes():
        raise RuntimeError("source config is not byte-identical to its frozen run config")
    source_state = _json(source_outputs / "corpus/state.json")
    if source_state.get("status") == "running":
        raise RuntimeError("source corpus is still running")
    source_report_path = source_outputs / "corpus/report.json"
    source_report = _json(source_report_path)

    provenance_dir = destination_artifacts / f"reuse-from-{source_run}"
    summary_path = destination_artifacts / f"reuse-from-{source_run}.json"
    legacy_manifest_path = provenance_dir / "files.jsonl"
    source_config_digest = _sha256_file(source_config_path)
    destination_config_digest = _sha256_file(destination_config_path)
    corpus_config = CorpusConfig.from_mapping(destination_config, repo_root=REPO_ROOT)
    scoring_contract_digest = corpus_config.scoring_contract_sha256
    scoring_implementation_digest = _sha256_file(RUNTIME_SOURCE)
    if summary_path.is_file():
        existing = _json(summary_path)
        expected_identity = {
            "source_run_id": source_run,
            "destination_run_id": destination_run,
            "source_config_sha256": source_config_digest,
            "destination_config_sha256": destination_config_digest,
            "scoring_contract_sha256": scoring_contract_digest,
            "scoring_implementation_sha256": scoring_implementation_digest,
        }
        if any(existing.get(key) != value for key, value in expected_identity.items()):
            raise RuntimeError("existing import summary does not match the current configs/scoring contract")
        if "file_manifest" not in existing and "file_manifest_sha256" not in existing:
            return existing

    import_id = _sha256_bytes(
        "\0".join(
            (
                source_run,
                destination_run,
                source_config_digest,
                destination_config_digest,
                scoring_contract_digest,
                scoring_implementation_digest,
            )
        ).encode("utf-8")
    )[:24]

    tasks = {
        task.task_id: _apply_scoring_rule(corpus_config, task)
        for task in load_tasks(corpus_config.dataset_path, expected_tasks=corpus_config.expected_tasks)
    }
    evaluator_path = corpus_config.evaluator_path
    source_case_paths = sorted((source_outputs / "corpus/cases").glob("*/case-*.json"))
    completed: list[
        tuple[Path, dict[str, Any], SpreadsheetTask, SpreadsheetCase, Path, ScoreResult, ScoreResult]
    ] = []
    excluded = Counter()
    excluded_ids: dict[str, list[str]] = defaultdict(list)

    for source_case_path in source_case_paths:
        record = _json(source_case_path)
        status = str(record.get("status") or "missing")
        if status != "completed":
            excluded[status] += 1
            excluded_ids[status].append(str(record.get("case_id") or source_case_path.stem))
            continue
        task_id = str(record.get("task_id") or "")
        case_index = int(record.get("case_index") or 0)
        task = tasks.get(task_id)
        if task is None or case_index not in {1, 2, 3}:
            raise ValueError(f"completed source record has invalid identity: {source_case_path}")
        case = task.cases[case_index - 1]
        safe_task = _safe_id(task_id)
        source_output = source_artifacts / "corpus/executions" / safe_task / f"case-{case_index}/output.xlsx"
        evidence = record.get("evidence") if isinstance(record.get("evidence"), Mapping) else {}
        if _sha256_file(source_output) != evidence.get("output_sha256"):
            raise RuntimeError(f"source output digest mismatch for {case.case_id}")
        result = _score_case(
            scorer,
            task=task,
            case=case,
            output_path=source_output,
            evaluator_path=evaluator_path,
        )
        baseline = _score_case(
            scorer,
            task=task,
            case=case,
            output_path=case.input_path,
            evaluator_path=evaluator_path,
        )
        completed.append((source_case_path, record, task, case, source_output, result, baseline))

    if len(completed) != int(source_report.get("cases_completed") or -1):
        raise RuntimeError("completed-case scan disagrees with the frozen source report")

    by_task: dict[
        str,
        list[tuple[Path, dict[str, Any], SpreadsheetTask, SpreadsheetCase, Path, ScoreResult, ScoreResult]],
    ] = defaultdict(list)
    for item in completed:
        by_task[item[2].task_id].append(item)

    plan = _Plan()
    raw_root = provenance_dir / "raw"
    plan.add(source_config_path, provenance_dir / "context/source-config.json", kind="run_context")
    plan.add(destination_config_path, provenance_dir / "context/destination-config.json", kind="run_context")
    plan.add(source_report_path, provenance_dir / "context/source-report.json", kind="run_context")
    plan.add(
        corpus_config.scoring_contract_path,
        provenance_dir / "context/scoring-contract.json",
        kind="scoring_contract",
    )
    transitions: list[dict[str, Any]] = []
    operational_models: dict[str, dict[str, Any]] = {}

    for task_id in sorted(by_task):
        safe_task = _safe_id(task_id)
        source_model_path = source_outputs / "corpus/model" / f"{safe_task}.json"
        source_solution_path = source_artifacts / "corpus/solutions" / f"{safe_task}.py"
        raw_model = _json(source_model_path)
        if raw_model.get("status") != "generated":
            raise RuntimeError(f"completed cases depend on a non-generated model record: {task_id}")
        solution = source_solution_path.read_text(encoding="utf-8")
        if _sha256_bytes(solution.encode("utf-8")) != raw_model.get("solution_sha256"):
            raise RuntimeError(f"solution digest mismatch for {task_id}")
        case_one = next((item for item in by_task[task_id] if item[3].case_index == 1), None)
        if case_one is None:
            raise RuntimeError(f"cannot reuse task without completed case-1 validation: {task_id}")
        rescored_model = copy.deepcopy(raw_model)
        execution = _final_execution(rescored_model)
        execution["score"] = case_one[5].score
        execution["passed"] = case_one[5].passed
        rescored_model["offline_rescoring"] = {
            "schema_version": 1,
            "import_id": import_id,
            "source_run_id": source_run,
            "destination_run_id": destination_run,
            "scoring_contract_sha256": scoring_contract_digest,
            "scoring_implementation_sha256": scoring_implementation_digest,
            "model_calls": 0,
            "executor_calls": 0,
        }
        operational_models[task_id] = rescored_model
        plan.add(source_model_path, raw_root / "outputs/corpus/model" / source_model_path.name, kind="raw_model")
        plan.add(
            source_model_path,
            destination_outputs / "corpus/model" / source_model_path.name,
            kind="rescored_model_metadata",
            payload=_json_bytes(rescored_model),
        )
        plan.add(
            source_solution_path,
            destination_artifacts / "corpus/solutions" / source_solution_path.name,
            kind="solution",
        )
        plan.add_tree(
            source_artifacts / "corpus/source" / safe_task,
            destination_artifacts / "corpus/source" / safe_task,
            kind="source_workbook",
        )

    for source_case_path, source_record, task, case, source_output, result, baseline in completed:
        safe_task = _safe_id(task.task_id)
        case_dir = f"case-{case.case_index}"
        source_execution = source_artifacts / "corpus/executions" / safe_task / case_dir
        destination_execution = destination_artifacts / "corpus/executions" / safe_task / case_dir
        plan.add_tree(source_execution, destination_execution, kind="execution_evidence")
        destination_output = destination_execution / "output.xlsx"
        source_evidence = source_record.get("evidence")
        if not isinstance(source_evidence, Mapping):
            raise ValueError(f"completed case has no evidence object: {case.case_id}")
        source_input = source_artifacts / "corpus/source" / safe_task / case.input_path.name
        source_answer = source_artifacts / "corpus/source" / safe_task / case.answer_path.name
        if _sha256_file(source_input) != source_evidence.get("input_sha256"):
            raise RuntimeError(f"source input digest mismatch for {case.case_id}")
        if _sha256_file(source_answer) != source_evidence.get("answer_sha256"):
            raise RuntimeError(f"source answer digest mismatch for {case.case_id}")

        operational_model = operational_models[task.task_id]
        carrier_eligible = _carrier_eligible(
            corpus_config,
            task,
            output_pass=result.passed,
            baseline_input_pass=baseline.passed,
        )
        solution = (source_artifacts / "corpus/solutions" / f"{safe_task}.py").read_text(encoding="utf-8")
        payload = build_session_payload(
            task=task,
            case=case,
            model_record=operational_model,
            solution=solution,
            outcome="success" if result.passed else "task_failure",
            score=result.score,
            passed=result.passed,
            execution_attempts=copy.deepcopy(source_record.get("attempts") or []),
            baseline_input_pass=baseline.passed,
            carrier_eligible=carrier_eligible,
        )
        old_payload = source_record.get("session_payload")
        if isinstance(old_payload, Mapping) and old_payload.get("timestamp"):
            payload["timestamp"] = old_payload["timestamp"]
        destination_record = copy.deepcopy(source_record)
        destination_record.update(
            status="completed",
            outcome="success" if result.passed else "task_failure",
            score=result.score,
            passed=result.passed,
            baseline_input_pass=baseline.passed,
            carrier_eligible=carrier_eligible,
            error_class=None if result.passed else "science_task",
            grading={
                "status": result.status,
                "message": result.message,
                "normalized_answer_position": result.normalized_answer_position,
                "baseline_input": {
                    "status": baseline.status,
                    "passed": baseline.passed,
                    "normalized_answer_position": baseline.normalized_answer_position,
                },
                "scoring_contract_rule": dict(corpus_config.scoring_rule(task.task_id)),
            },
            session_payload=payload,
            evidence={
                "input_sha256": _sha256_file(source_input),
                "answer_sha256": _sha256_file(source_answer),
                "output_sha256": _sha256_file(source_output),
                "output_path": str(destination_output),
            },
            offline_reuse={
                "schema_version": 1,
                "import_id": import_id,
                "source_run_id": source_run,
                "destination_run_id": destination_run,
                "source_case_record_sha256": _sha256_file(source_case_path),
                "source_score": source_record.get("score"),
                "source_passed": source_record.get("passed"),
                "scoring_contract_sha256": scoring_contract_digest,
                "scoring_implementation_sha256": scoring_implementation_digest,
                "model_calls": 0,
                "executor_calls": 0,
            },
        )
        case_record_to_session(operational_model, destination_record)
        destination_case_path = destination_outputs / "corpus/cases" / safe_task / source_case_path.name
        plan.add(
            source_case_path,
            raw_root / "outputs/corpus/cases" / safe_task / source_case_path.name,
            kind="raw_case",
        )
        plan.add(
            source_case_path,
            destination_case_path,
            kind="rescored_case_metadata",
            payload=_json_bytes(destination_record),
        )
        if (
            source_record.get("score") != result.score
            or source_record.get("passed") != result.passed
            or (source_record.get("grading") or {}).get("normalized_answer_position")
            != result.normalized_answer_position
        ):
            transitions.append(
                {
                    "case_id": case.case_id,
                    "source_score": source_record.get("score"),
                    "destination_score": result.score,
                    "source_passed": source_record.get("passed"),
                    "destination_passed": result.passed,
                    "source_answer_position": (source_record.get("grading") or {}).get(
                        "normalized_answer_position"
                    ),
                    "destination_answer_position": result.normalized_answer_position,
                }
            )

    file_records = plan.commit()
    score_counts = Counter(str(item[5].score) for item in completed)
    summary = {
        "schema_version": 1,
        "status": "complete",
        "mode": "offline_provenance_preserving_rescore",
        "import_id": import_id,
        "source_run_id": source_run,
        "destination_run_id": destination_run,
        "source_config_sha256": source_config_digest,
        "destination_config_sha256": destination_config_digest,
        "scoring_contract_sha256": scoring_contract_digest,
        "scoring_implementation_sha256": scoring_implementation_digest,
        "model_calls": 0,
        "executor_calls": 0,
        "completed_cases_imported": len(completed),
        "tasks_represented": len(by_task),
        "destination_scores": dict(sorted(score_counts.items())),
        "excluded": {key: sorted(value) for key, value in sorted(excluded_ids.items())},
        "score_or_range_transitions": transitions,
        "files_recorded": len(file_records),
        "aggregate_provenance_sha256": _sha256_bytes(
            "".join(
                f"{item['destination']}\0{item['destination_sha256']}\n" for item in file_records
            ).encode("utf-8")
        ),
        "source_report_sha256": _sha256_file(source_report_path),
        "policy": {
            "completed_only": True,
            "unknown_and_aborted_excluded": True,
            "raw_case_and_model_records_preserved": True,
            "operational_scoring_metadata_regenerated": True,
            "old_task_state_imported": False,
            "old_report_imported_as_destination_state": False,
        },
    }
    summary_payload = _json_bytes(summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    if summary_path.exists() and summary_path.read_bytes() != summary_payload:
        previous = _json(summary_path)
        if "file_manifest" not in previous and "file_manifest_sha256" not in previous:
            raise RuntimeError("refusing to overwrite a differing import summary")
    summary_path.write_bytes(summary_payload)
    # An early development version wrote a per-file hash inventory.  Keep the
    # compact aggregate provenance above and remove that redundant list.
    legacy_manifest_path.unlink(missing_ok=True)
    return summary


def derive_completed_corpus(
    source_config_path: str | Path, destination_config_path: str | Path
) -> dict[str, Any]:
    """Reuse all determinate cases with a new input view, without re-scoring.

    Only adapted case metadata, a frozen config, state/report and one provenance
    record are written. Workbooks, model logs and original case evidence remain
    at their frozen source paths. This is explicitly not a new collection run.
    """

    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    source_config_path, destination_config_path = _resolve(source_config_path), _resolve(destination_config_path)
    source_config, destination_config = _json(source_config_path), _json(destination_config_path)
    source_run, source_artifacts, source_outputs = _run_roots(source_config)
    destination_run, destination_artifacts, destination_outputs = _run_roots(destination_config)
    if source_run == destination_run or source_outputs.resolve() == destination_outputs.resolve():
        raise ValueError("offline projection requires a distinct destination corpus")
    adapter = (destination_config.get("corpus") or {}).get("session_adapter")
    if adapter not in {"execution_facts_v2", "execution_facts_v3"}:
        raise ValueError("offline projection requires corpus.session_adapter=execution_facts_v2 or execution_facts_v3")
    for key in ("dataset_sha256", "selection_sha256", "evaluator_sha256", "scoring_contract_sha256"):
        before, after = source_config.get("sources", {}).get(key), destination_config.get("sources", {}).get(key)
        if not before or before != after:
            raise ValueError(f"offline projection cannot change scientific acquisition/scoring identity: {key}")
    frozen_source = source_artifacts / "frozen-config.json"
    if frozen_source.read_bytes() != source_config_path.read_bytes():
        raise RuntimeError("source config differs from the frozen acquisition config")
    source_state = _json(source_outputs / "corpus/state.json")
    source_report_path = source_outputs / "corpus/report.json"
    source_report = _json(source_report_path)
    expected_tasks = int(destination_config.get("corpus", {}).get("task_count") or 100)
    expected_cases = expected_tasks * 3
    if (
        source_state.get("status") != "complete"
        or source_report.get("status") != "complete"
        or source_report.get("cases_completed") != expected_cases
        or source_report.get("cases_unknown") != 0
        or not source_report.get("ready_for_trials")
    ):
        raise RuntimeError("offline projection requires a complete, ready, determinate source corpus")
    identity = {
        "source_run_id": source_run,
        "destination_run_id": destination_run,
        "source_config_sha256": _sha256_file(source_config_path),
        "destination_config_sha256": _sha256_file(destination_config_path),
        "source_report_sha256": _sha256_file(source_report_path),
        "session_adapter": adapter,
    }
    summary_path = destination_artifacts / "corpus/derivation.json"
    if summary_path.is_file():
        existing = _json(summary_path)
        if any(existing.get(key) != value for key, value in identity.items()):
            raise RuntimeError("existing corpus derivation has a different identity")
        report = _json(destination_outputs / "corpus/report.json")
        state = _json(destination_outputs / "corpus/state.json")
        if (report.get("derivation_identity") != identity or state.get("status") != "complete"
                or len(list((destination_outputs / "corpus/cases").glob("*/case-*.json"))) != expected_cases):
            raise RuntimeError("completed derivation is missing its corpus evidence")
        return existing
    case_paths = sorted((source_outputs / "corpus/cases").glob("*/case-*.json"))
    if len(case_paths) != expected_cases:
        raise RuntimeError("source case count disagrees with the frozen corpus report")
    prepared: list[tuple[Path, dict[str, Any]]] = []
    identifiers: set[str] = set()
    task_cases: dict[str, set[int]] = defaultdict(set)
    for source_path in case_paths:
        record = _json(source_path)
        if (record.get("status") != "completed" or not isinstance(record.get("passed"), bool)
                or record.get("score") not in {0.0, 1.0}):
            raise RuntimeError(f"source case is not determinate: {source_path}")
        session = case_record_to_session(None, record)
        if session.get("score") != record.get("score") or session.get("passed") != record.get("passed"):
            raise RuntimeError(f"source session and recorded grading disagree: {source_path}")
        case_index = int(record["case_index"])
        if case_index not in {1, 2, 3} or session["session_id"] in identifiers:
            raise RuntimeError("source corpus has invalid/duplicate case identities")
        identifiers.add(session["session_id"])
        task_cases[str(record["task_id"])].add(case_index)
        evidence = record.get("evidence") or {}
        session["collection_evidence"]["case_record_facts"] = {
            "output_exists": bool(evidence.get("output_sha256") and evidence.get("output_path")),
            "source_case_record": _relative(source_path),
        }
        if adapter == "execution_facts_v3":
            session["collection_evidence"]["case_record_facts"]["grading"] = copy.deepcopy(record.get("grading"))
        destination_record = copy.deepcopy(record)
        destination_record["session_payload"] = adapt_session_for_evolution(session, adapter=adapter)
        destination_record["offline_derivation"] = {
            "source_run_id": source_run,
            "source_case_record": _relative(source_path),
            "session_adapter": adapter,
            "model_calls": 0, "executor_calls": 0, "scorer_calls": 0,
        }
        prepared.append((destination_outputs / "corpus/cases" / source_path.parent.name / source_path.name,
                         destination_record))
    if len(task_cases) != expected_tasks or any(indices != {1, 2, 3} for indices in task_cases.values()):
        raise RuntimeError("source corpus must contain exactly three distinct cases per task")
    for metric, observed in (
        ("cases_passed", sum(record["passed"] is True for _, record in prepared)),
        ("carrier_eligible_case_count", sum(record.get("carrier_eligible") is True for _, record in prepared)),
    ):
        if source_report.get(metric) != observed:
            raise RuntimeError(f"source case records disagree with frozen report: {metric}")

    def write_once(path: Path, payload: bytes) -> None:
        if path.is_file():
            if path.read_bytes() != payload:
                raise RuntimeError(f"refusing to overwrite a different derived artifact: {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
        temporary.write_bytes(payload)
        os.replace(temporary, path)

    write_once(destination_artifacts / "frozen-config.json", destination_config_path.read_bytes())
    for path, record in prepared:
        write_once(path, _json_bytes(record))
    finished_at = datetime.now(timezone.utc).isoformat()
    common = {
        "run_id": destination_run,
        "status": "complete",
        "mode": "offline_execution_fact_projection",
        "source_run_id": source_run,
        "session_adapter": adapter,
        "model_requests": 0,
        "accounted_tokens": 0,
        "executor_calls": 0,
        "scorer_calls": 0,
        "elapsed_seconds": time.monotonic() - started,
        "started_at": started_at,
        "stop_reason": None,
    }
    existing_report = destination_outputs / "corpus/report.json"
    if existing_report.is_file():
        prior = _json(existing_report)
        if prior.get("derivation_identity") != identity:
            raise RuntimeError("existing derived report belongs to a different projection")
        for key in ("elapsed_seconds", "started_at"):
            common[key] = prior[key]
        finished_at = prior["finished_at"]
    report = copy.deepcopy(source_report)
    report.update(common, finished_at=finished_at, artifacts=str(destination_artifacts / "corpus"),
                  outputs=str(destination_outputs / "corpus"),
                  derivation_identity=identity,
                  source_report=_relative(source_report_path),
                  source_collection_elapsed_seconds=source_report.get("elapsed_seconds"))
    state = {"schema_version": 1, **common, "updated_at": finished_at, "abort_requested": False}
    write_once(destination_outputs / "corpus/report.json", _json_bytes(report))
    write_once(destination_outputs / "corpus/state.json", _json_bytes(state))
    summary = {
        "schema_version": 1, "status": "complete", "ready_for_trials": True, "mode": common["mode"], **identity,
        "completed_cases_imported": len(prepared), "tasks_represented": len(task_cases),
        "model_calls": 0, "executor_calls": 0, "scorer_calls": 0,
        "source_report": _relative(source_report_path),
        "source_case_root": _relative(source_outputs / "corpus/cases"),
        "workbooks_copied": 0, "scientific_grading_preserved": True,
    }
    write_once(summary_path, _json_bytes(summary))
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-config", required=True)
    parser.add_argument("--destination-config", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = import_completed_corpus(args.source_config, args.destination_config)
    except Exception as exc:
        print(f"offline corpus import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
