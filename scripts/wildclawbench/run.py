#!/usr/bin/env python3
"""Resumable, configured Day/Night experiment with real task validation."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.metadata
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from scripts.injection import resilient_runner
from scripts.wildclawbench.protocol import (
    ROOT,
    REPOSITORY,
    execution_root,
    output_root,
    accept_candidate,
    alignment_candidate_id,
    alignment_settings,
    canonical_hash,
    create_initial_bank,
    discover_tasks,
    fixed_execution_count,
    load_config,
    public_config,
    tree_hash,
    write_json,
)
from scripts.wildclawbench.reports import generate


LOG_LOCK = threading.Lock()
RUN_OUTPUTS: dict[Path, Path] = {}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def event(run_dir: Path, kind: str, **fields) -> None:
    record = {"timestamp": now(), "event": kind, **fields}
    with LOG_LOCK:
        with (RUN_OUTPUTS.get(run_dir.resolve(), run_dir) / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        # No model prompts or secrets in console logs.
        print(json.dumps(record, ensure_ascii=False), flush=True)


@contextlib.contextmanager
def exclusive_run(run_dir: Path):
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "controller.lock").open("w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("A controller is already running for this experiment") from error
        yield


def update_state(run_dir: Path, status: str, **fields):
    previous = json.loads((run_dir / "state.json").read_text()) if (run_dir / "state.json").exists() else {}
    previous.update(status=status, updated_at=now(), **fields)
    if status != "needs_attention":
        previous.pop("error", None)
    write_json(run_dir / "state.json", previous)


def freeze(config: dict, tasks: list[dict], run_dir: Path):
    # Files, seeds and interfaces are immutable within one run. Keys are not persisted.
    sources = {}
    source_paths = list(ROOT.glob("*.py"))
    source_paths += list((ROOT / "services").glob("*.py"))
    source_paths += list((ROOT / "operations").glob("*.py"))
    if config.get("native_night_flow"):
        source_paths += [REPOSITORY / "scripts/injection" / name for name in
                         ("native_night_flow.py", "source_evidence_v2.py", "legacy_native_receipt.py")]
    declared_sources = config.get("freeze_source_files", [])
    if (not isinstance(declared_sources, list)
            or any(not isinstance(item, str) or not item for item in declared_sources)):
        raise ValueError("freeze_source_files must be a list of repository file paths")
    for declared in declared_sources:
        supplied = Path(declared)
        supplied = supplied if supplied.is_absolute() else REPOSITORY / supplied
        path = supplied.resolve()
        if (supplied.is_symlink() or not path.is_relative_to(REPOSITORY.resolve()) or not path.is_file()):
            raise ValueError("freeze_source_files must name regular files inside the repository")
        source_paths.append(path)
    experiment_root = Path(config.get("experiment_root", ROOT)).resolve()
    if experiment_root != ROOT.resolve():
        source_paths += list(experiment_root.glob("*.py"))
        source_paths += list((experiment_root / "services").glob("*.py"))
    for package in ("sharedskill", "evolve_server"):
        source_paths += list((REPOSITORY / package).rglob("*.py"))
    # The native evolver reads this prompt at night; freezing only Python would
    # allow its instructions to change without invalidating a resumed trial.
    source_paths.append(REPOSITORY / "evolve_server/engines/EVOLVE_AGENTS.md")
    for path in sorted(set(source_paths)):
        name = (
            path.relative_to(REPOSITORY).as_posix()
            if path.is_relative_to(REPOSITORY) else str(path.resolve())
        )
        raw = path.read_bytes()
        sources[name] = hashlib.sha256(raw).hexdigest()
        snapshot = run_dir / "source" / (name if not Path(name).is_absolute() else "external/" + name.lstrip("/"))
        if snapshot.exists() and snapshot.read_bytes() != raw:
            raise ValueError("Frozen source bytes changed; use a new experiment ID")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if not snapshot.exists():
            snapshot.write_bytes(raw)
    document = {"config": public_config(config), "tasks": tasks, "source_hashes": sources}
    alignment = alignment_settings(config)
    if alignment:
        document["alignment_policy"] = alignment
        document["context_policy"] = {
            "compaction_mode": alignment["compaction_mode"],
            "context_pruning": alignment["context_pruning"],
            "evolution_preprocessing": "native-programmatic-trajectory-and-llm-summary-v1",
            "uncompressed_context_claimed": False,
            "context_length": int(config.get("context_length", 131072)),
            "output_reserve_tokens": int(config.get("max_tokens", 16384)),
            "agent_tokenizer_url": config.get("agent_tokenizer_url")
            or config["model_base_url"].rstrip("/").removesuffix("/v1") + "/tokenize",
        }
    destination = run_dir / "frozen-protocol.json"
    if destination.exists():
        if json.loads(destination.read_text()) != document:
            raise ValueError("Configuration or task bytes changed; use a new run instead of reusing results")
    else:
        write_json(destination, document)
    inventory = run_dir / "startup-inventory.json"
    if not inventory.exists():

        def command(args):
            process = subprocess.run(args, cwd=REPOSITORY, capture_output=True, text=True, check=False)
            return {"exit_code": process.returncode, "stdout": process.stdout.strip(), "stderr": process.stderr.strip()}

        versions = {}
        for name in ("openai", "httpx", "fastapi", "uvicorn", "pyyaml", "matplotlib", "huggingface-hub"):
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                versions[name] = None
        write_json(
            inventory,
            {
                "started_at": now(),
                "git_commit": command(["git", "rev-parse", "HEAD"]),
                "git_status": command(["git", "status", "--short"]),
                "experiment_source_hashes": sources,
                "python": sys.version,
                "packages": versions,
                "gpu": command(
                    [
                        "nvidia-smi",
                        "--query-gpu=index,uuid,name,memory.total,memory.free,driver_version",
                        "--format=csv",
                    ]
                ),
                "fixed_execution_count": fixed_execution_count(tasks, config),
                "pilot_execution_count": sum(t["short_id"] in config.get("pilot_short_ids", []) for t in tasks),
            },
        )
    return canonical_hash(document)


def check_services(config: dict):
    health_url = config["local_api_base_url"].removesuffix("/v1") + "/healthz"
    response = httpx.get(health_url, timeout=10, trust_env=False)
    response.raise_for_status()
    status = response.json()
    if not status.get("ready"):
        raise RuntimeError(f"Local capabilities are not ready: {status.get('roles')}")


def _recovery_json(reference: dict) -> dict:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ValueError("Incomplete execution recovery reference")
    path = Path(reference["path"])
    if (not path.is_absolute() or path.resolve() != path or path.is_symlink() or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]):
        raise ValueError("Execution recovery reference changed")
    return json.loads(path.read_text())


def _recovery_files(directory: Path) -> list[dict]:
    if directory.is_symlink() or not directory.is_dir() or directory.resolve() != directory:
        raise ValueError("Execution recovery requires the original actual output directory")
    records = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlink in frozen failed execution")
        if path.is_file():
            records.append({"path": path.relative_to(directory).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": path.stat().st_mode & 0o777})
        elif not path.is_dir():
            raise ValueError("Non-regular entry in frozen failed execution")
    return records


def _execution_recovery(original: Path, identity: dict, task: dict, run_dir: Path, config: dict) -> dict | None:
    """Select only an explicit independent attempt; never rewrite failed evidence."""
    amendment = config.get("source_amendment")
    if amendment is None:
        return None
    source_ref = {key: amendment[key] for key in ("path", "sha256")}
    document = _recovery_json(source_ref)
    if (document.get("policy") != "correction-fork-source-amendment-v1"
            or document.get("amendment_hash") != canonical_hash({k: v for k, v in document.items() if k != "amendment_hash"})):
        raise ValueError("Execution recovery amendment digest changed")
    recoveries = document.get("execution_recoveries", {})
    if not isinstance(recoveries, dict):
        raise ValueError("Invalid execution recovery map")
    if document.get("epoch") == "collector-scope-boundary-v1":
        previous_ref = document.get("previous_source_amendment")
        expected_path = (Path(document["run_dir"]).parents[1] /
            "operations/flow-fix-20261017/per-skill-fix/dependency-path-fix/context-recovery-fix/source-amendment-v2.json")
        if previous_ref != {"path": str(expected_path), "sha256":
                "f5bf6fe6891b3a2eaa1f7717bd04e90855440a8badf75a9c16d4b5aef5db4b4d"}:
            raise ValueError("Collector scope recovery must pin its actual previous recovery epoch")
        previous = _recovery_json(previous_ref)
        prior, effective = previous.get("sources"), document.get("sources")
        if (previous.get("epoch") != "recovered-context-boundary-v1"
                or previous.get("policy") != "correction-fork-source-amendment-v1"
                or previous.get("amendment_hash") != canonical_hash({k: v for k, v in previous.items()
                                                                     if k != "amendment_hash"})
                or previous.get("base_config") != document.get("base_config")
                or previous.get("run_dir") != document.get("run_dir")
                or previous.get("collections") != document.get("collections")
                or previous.get("execution_recoveries") != recoveries
                or not isinstance(prior, list) or not isinstance(effective, list)
                or len(prior) != 146 or len(effective) != 146
                or any(set(row) != {"path", "sha256"} for row in effective)
                or [row["path"] for row in prior] != [row["path"] for row in effective]
                or len({row["path"] for row in effective}) != 146):
            raise ValueError("Collector scope recovery changed its inherited immutable execution evidence")
        flow = [row["path"] for row in prior if row["path"].endswith("/scripts/injection/flow_fix.py")]
        delta_records = [{"path": before["path"], "before_sha256": before["sha256"], "after_sha256": after["sha256"]}
                         for before, after in zip(prior, effective, strict=True) if before != after]
        delta = {row["path"] for row in delta_records}
        if (len(flow) != 1 or delta != {flow[0], str(Path(__file__).resolve())}
                or document.get("previous_source_delta") != delta_records):
            raise ValueError("Collector scope recovery changed sources outside its reviewed flow/run delta")
        # Reuse the original selector and its v1/receipt/file-map checks. Only this
        # private call receives v2; the caller's actual task config stays in v3.
        return _execution_recovery(original, identity, task, run_dir,
                                   {**config, "source_amendment": previous_ref})
    if not recoveries:
        return None
    if document.get("epoch") != "recovered-context-boundary-v1":
        raise ValueError("Independent execution recovery requires its explicit source epoch")
    previous = _recovery_json(document.get("previous_source_amendment"))
    if (previous.get("policy") != "correction-fork-source-amendment-v1"
            or previous.get("epoch") != "dependency-path-boundary-v1"
            or previous.get("amendment_hash") != canonical_hash({k: v for k, v in previous.items() if k != "amendment_hash"})
            or previous.get("base_config") != document.get("base_config")):
        raise ValueError("Independent recovery lost its pinned previous source amendment")
    receipts = {key: _recovery_json(value) for key, value in recoveries.items()}
    receipt = receipts.get(str(original))
    if receipt is None:
        return None
    new_output = Path(receipt.get("new_output", ""))
    recovery_id = receipt.get("recovery_id")
    if (receipt.get("policy") != "explicit-independent-task-recovery-v1"
            or not isinstance(recovery_id, str) or not recovery_id
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in recovery_id)
            or recovery_id in {".", ".."} or receipt.get("original_output") != str(original)
            or not original.is_relative_to(run_dir.resolve())
            or not new_output.is_absolute() or new_output.resolve() != new_output or new_output.is_symlink()
            or not new_output.is_relative_to(run_dir.resolve()) or new_output == original
            or new_output.is_relative_to(original) or original.is_relative_to(new_output)):
        raise ValueError("Unsafe or mismatched independent recovery output identity")
    result_ref = receipt.get("original_result")
    failed = _recovery_json(result_ref)
    original_hash = canonical_hash(identity)
    expected = {"execution_hash": original_hash, "phase": identity["phase"], "round": identity["round"],
        "arm": identity["arm"], "candidate_id": identity["candidate_id"], "task_id": task["task_id"],
        "short_id": task["short_id"], "user_id": task["user_id"], "seed": identity["seed"],
        "skill_bank_sha256": identity["bank"], "input_sha256": identity["input"]}
    if (result_ref["path"] != str(original / "result.json")
            or receipt.get("original_execution_hash") != original_hash
            or failed.get("status") != "infrastructure_error" or failed.get("score") is not None
            or any(failed.get(key) != value for key, value in expected.items())
            or _recovery_files(original) != receipt.get("original_files")):
        raise ValueError("Original failed result, execution identity or complete file map changed")
    return {**recoveries[str(original)], "recovery_id": recovery_id,
        "original_output": str(original), "new_output": str(new_output), "original_result": result_ref,
        "original_execution_hash": original_hash, "source_amendment": source_ref}


def run_one(
    task: dict, bank: Path, run_dir: Path, config: dict, phase: str, number: int, arm: str = "", candidate_id: str = ""
) -> dict:
    from scripts.wildclawbench.task_runtime import execute_task, rescore_task

    input_proof = None
    if task.get("input_sha256"):
        from scripts.wildclawbench.provenance import validate_task_inputs

        input_proof = validate_task_inputs(config, task)
    bank_hash = tree_hash(bank)
    identity = {
        "phase": phase,
        "round": number,
        "arm": arm,
        "candidate_id": candidate_id,
        "task_id": task["task_id"],
        "bank": bank_hash,
        "config": config["protocol_hash"],
        "input": task.get("input_sha256", task["source_sha256"]),
        "seed": config["seed"],
    }
    directory = (
        execution_root(run_dir, config)
        / phase
        / f"{number:02d}"
        / (candidate_id or "main")
        / (arm or "shared")
        / task["short_id"]
    )
    recovery = _execution_recovery(directory.resolve(), identity, task, run_dir, config)
    if recovery:
        identity["recovery_id"] = recovery["recovery_id"]
        directory = Path(recovery["new_output"])
        if directory.exists() and not (directory / "result.json").is_file():
            raise RuntimeError("Independent recovery already started without a sealed result; reconcile its original process")
        if input_proof is None:
            from scripts.wildclawbench.provenance import validate_task_inputs
            input_proof = validate_task_inputs(config, task)
    execution_hash = canonical_hash(identity)
    directory.mkdir(parents=True, exist_ok=True)
    if recovery:
        write_json(directory / "input-validation.json", input_proof)
    result_path = directory / "result.json"
    previous = json.loads(result_path.read_text()) if result_path.exists() else None
    if previous and previous.get("execution_hash") != execution_hash:
        raise ValueError(f"Refusing cross-bank/config result reuse: {directory}")
    if recovery and previous:
        if previous.get("execution_recovery") != recovery:
            raise ValueError("Independent recovery result belongs to another immutable receipt")
        if previous.get("status") not in {"completed", "pending_score"}:
            raise RuntimeError("Independent Agent recovery allowance is used; refusing another Agent call")
    if previous and previous.get("status") == "completed":
        if alignment_settings(config):
            from scripts.wildclawbench.alignment_catalog import inspect_bank, load_binding

            saved = json.loads((directory / "execution-binding.json").read_text())
            run_id = "wcb-" + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:20]
            registered = load_binding(config, run_id, execution_hash[:24])
            if saved != registered:
                raise ValueError("Cached result differs from its immutable execution binding")
            current = inspect_bank(bank)
            if any(current[key] != registered[key] for key in ("bank_sha256", "files", "directories", "skills")):
                raise ValueError("Supplied bank manifest changed before completed result reuse")
        return previous
    started = previous.get("started_at", now()) if previous else now()
    task_config = {
        **config,
        "task": task,
        "user_id": task["user_id"],
        "phase": phase,
        "round": number,
        "seed": config["seed"],
        "execution_id": execution_hash[:24],
    }
    if resilient_runner.enabled(config):
        budget = resilient_runner.active_budget(config)
        if budget is not None:
            task_config = budget.cap_config(task_config, f"{phase}:{number}:{candidate_id or 'main'}:{task['short_id']}")
    if config.get("output_run_dir"):
        task_config["log_dir"] = str(output_root(run_dir, config) / "logs" / "executions" / phase
            / f"{number:02d}" / (candidate_id or "main") / (arm or "shared") / task["short_id"])
    if recovery:
        task_config["execution_recovery"] = recovery
    event(
        run_dir,
        "task_started",
        phase=phase,
        round=number,
        arm=arm,
        task=task["short_id"],
        candidate_id=candidate_id,
        bank_sha256=bank_hash,
    )
    result = {}
    attempt_seconds = 0.0
    attempts = 1 if recovery else config["infrastructure_retries"] + 1
    for attempt in range(attempts):
        attempt_start = time.monotonic()
        try:
            if previous and previous.get("status") == "pending_score":
                result = rescore_task(task["task_id"], directory, task_config)
            else:
                result = execute_task(task["task_id"], bank, directory, task_config)
        except Exception as error:
            result = {"status": "infrastructure_error", "score": None, "error": f"{type(error).__name__}: {error}"}
        attempt_seconds += time.monotonic() - attempt_start
        if result.get("status") != "infrastructure_error":
            break
        if attempt < attempts - 1:
            event(
                run_dir,
                "infrastructure_retry",
                phase=phase,
                task=task["short_id"],
                attempt=attempt + 1,
                error=result.get("error"),
            )
    if tree_hash(bank) != bank_hash:
        raise RuntimeError("Frozen bank was modified during task execution")
    score = result.get("score", result.get("overall_score"))
    if score is not None and not 0 <= float(score) <= 1:
        raise ValueError(f"Invalid normalized benchmark score: {score}")
    if score is not None:
        score = float(score)
    result.update(
        experiment_record=True,
        execution_hash=execution_hash,
        phase=phase,
        round=number,
        arm=arm,
        candidate_id=candidate_id,
        user_id=task["user_id"],
        task_id=task["task_id"],
        short_id=task["short_id"],
        category=task["category"],
        score=score,
        skill_bank_sha256=bank_hash,
        input_sha256=task.get("input_sha256", task["source_sha256"]),
        task_source_sha256=task["source_sha256"],
        started_at=started,
        finished_at=now(),
        output_dir=str(directory),
        seed=config["seed"],
    )
    result["last_attempt_wall_seconds"] = attempt_seconds
    if recovery:
        result.update(execution_recovery=recovery, original_execution_hash=recovery["original_execution_hash"],
                      independent_agent_attempt=True)
    result["wall_seconds"] = (previous.get("wall_seconds", 0) if previous else 0) + attempt_seconds
    result.setdefault("timed_out", False)
    result.setdefault("tool_errors", 0)
    session = result.setdefault("session", {})
    session.update(
        task_id=task["task_id"],
        session_id=execution_hash[:24],
        user_id=task["user_id"],
        phase=phase,
        round=number,
        skill_bank_sha256=bank_hash,
        outcome={"score": score, "timed_out": result["timed_out"], "tool_errors": result["tool_errors"]},
    )
    write_json(result_path, result)
    event(
        run_dir,
        "task_finished",
        phase=phase,
        round=number,
        arm=arm,
        task=task["short_id"],
        status=result["status"],
        score=score,
        seconds=result["wall_seconds"],
    )
    return result


def run_stage(
    tasks: list[dict],
    bank: Path,
    run_dir: Path,
    config: dict,
    phase: str,
    number: int,
    arm: str = "",
    candidate_id: str = "",
) -> list[dict]:
    resilient = resilient_runner.execution_protocol(config)
    if resilient:
        bank_hash = tree_hash(bank)
        cases = [resilient_runner.Case(task["short_id"], {
            "task_id": task["task_id"], "short_id": task["short_id"],
            "source_sha256": task["source_sha256"], "input_sha256": task.get("input_sha256"),
            "bank_sha256": bank_hash, "protocol_hash": config["protocol_hash"],
            "phase": phase, "round": number, "arm": arm, "candidate_id": candidate_id,
        }, task) for task in tasks]
        queue_id = canonical_hash({"phase": phase, "round": number, "arm": arm,
                                   "candidate_id": candidate_id, "bank": bank_hash})[:24]
        def execute_case(case):
            result = run_one(case.payload, bank, run_dir, config, phase, number, arm, candidate_id)
            result_path = Path(result["output_dir"]) / "result.json"
            retry_eligible = True
            retry_reason = None
            if result.get("status") == "infrastructure_error":
                execution_path = Path(result["output_dir"]) / "execution.json"
                try:
                    execution = json.loads(execution_path.read_text())
                except FileNotFoundError:
                    execution = None
                except (OSError, json.JSONDecodeError):
                    execution = {"status": "unreadable_agent_execution_evidence"}
                if execution is not None and execution.get("status") != "agent_completed":
                    # An incomplete/failed Agent execution cannot be retried:
                    # that would be a second stochastic task execution.  A
                    # completed Agent snapshot stays eligible because the task
                    # runtime enters replay-only scoring when execution.json is
                    # already sealed as agent_completed.
                    retry_eligible = False
                    retry_reason = "agent_started_evidence_sealed"
                elif execution is not None:
                    retry_reason = "frozen_agent_output_score_recovery"
            return {"status": result.get("status"), "score": result.get("score"),
                    "task_id": result.get("task_id"), "short_id": result.get("short_id"),
                    "error_type": result.get("error_type"), "error": result.get("error"),
                    "infrastructure_retry_eligible": retry_eligible,
                    "infrastructure_retry_reason": retry_reason,
                    "result_reference": {"path": str(result_path.resolve()),
                        "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest()}}

        def classify_case(result):
            classification = resilient_runner.classify_result(result)
            if (classification.status == "transient"
                    and result.get("infrastructure_retry_eligible") is False):
                return resilient_runner.Classification(
                    "terminal_transient", classification.error_type, classification.error)
            return classification

        def recover_interrupted_case(case, _record):
            """Allow resume only when another stochastic Agent call is impossible."""
            directory = (
                execution_root(run_dir, config)
                / phase
                / f"{number:02d}"
                / (candidate_id or "main")
                / (arm or "shared")
                / case.key
            )
            if not directory.exists():
                # The queue submitted the callback, but run_one never created
                # its execution directory, so no Agent process was started.
                return True
            if directory.is_symlink() or not directory.is_dir():
                return False
            result_path = directory / "result.json"
            try:
                prior = json.loads(result_path.read_text()) if result_path.is_file() else None
            except (OSError, json.JSONDecodeError):
                return False
            if isinstance(prior, dict) and prior.get("status") == "completed":
                # run_one verifies the frozen execution identity before reuse.
                return True
            execution_path = directory / "execution.json"
            try:
                execution = json.loads(execution_path.read_text())
            except (OSError, json.JSONDecodeError):
                return False
            # execute_task detects execution.json and enters replay-only
            # scoring; supervised Day recovery verifies the frozen snapshot.
            return execution.get("status") == "agent_completed"

        outcomes = resilient_runner.run_case_queue(cases, execute_case,
            run_dir / "operations/resilient-queues" / (queue_id + ".json"),
            max_workers=resilient["max_concurrency"],
            max_transient_retries=resilient["max_transient_retries"],
            same_signature_limit=resilient["circuit_breaker"]["same_signature_limit"],
            classify=classify_case,
            budget=resilient_runner.active_budget(config),
            recover_interrupted=recover_interrupted_case)
        results = []
        for task in tasks:
            outcome = outcomes[task["short_id"]]
            queued = outcome.get("result", {})
            reference = queued.get("result_reference")
            if outcome["status"] == "completed" and isinstance(reference, dict):
                path = Path(reference.get("path", ""))
                expected_root = execution_root(run_dir, config).resolve()
                if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(expected_root)
                        or hashlib.sha256(path.read_bytes()).hexdigest() != reference.get("sha256")):
                    raise resilient_runner.ProtocolError("Queued result reference changed or escaped its execution root")
                result = json.loads(path.read_text())
                if (result.get("task_id"), result.get("short_id")) != (task["task_id"], task["short_id"]):
                    raise resilient_runner.ProtocolError("Queued result reference belongs to another task")
            else:
                result = {"status": "infrastructure_error" if outcome["status"] == "circuit_open" else "failed",
                    "score": None, "task_id": task["task_id"], "short_id": task["short_id"],
                    "error_type": "CircuitBreakerOpen" if outcome["status"] == "circuit_open" else "CaseFailure",
                    "error": str(outcome.get("reason", queued.get("error", "case did not complete")))}
            results.append(result)
        if config.get("execution_plan") != "longitudinal-day-night-text-endpoint-v1":
            generate(run_dir, config["all_tasks"])
        pending = [row for row in results if row.get("status") != "completed" or row.get("score") is None]
        if pending:
            circuits = [row for row in outcomes.values() if row.get("status") == "circuit_open"]
            if circuits:
                raise resilient_runner.CircuitBreakerOpen(
                    f"{phase} {number}: infrastructure circuit opened; no further cases were scheduled")
            error = RuntimeError(f"{phase} {number}: {len(pending)} isolated cases incomplete; checkpoint is resumable")
            error.case_outcomes = outcomes
            raise error
        return sorted(results, key=lambda row: row["short_id"])
    # One worker per fixed simulated user; each user's tasks are sequential.
    assignments = {f"user-{i + 1:02d}": [] for i in range(config["users"])}
    for task in tasks:
        assignments[task["user_id"]].append(task)

    def worker(user_tasks):
        return [run_one(task, bank, run_dir, config, phase, number, arm, candidate_id) for task in user_tasks]

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=config["users"]) as pool:
        jobs = [pool.submit(worker, rows) for rows in assignments.values() if rows]
        for job in concurrent.futures.as_completed(jobs):
            results.extend(job.result())
    if config.get("execution_plan") != "longitudinal-day-night-text-endpoint-v1":
        generate(run_dir, config["all_tasks"])
    pending = [r for r in results if r["status"] != "completed" or r["score"] is None]
    if pending:
        raise RuntimeError(
            f"{phase} {number}: {len(pending)} tasks need infrastructure/score repair; "
            "resume reuses completed executions"
        )
    return sorted(results, key=lambda r: r["short_id"])


def outcome_preference(old: list[dict], new: list[dict], directory: Path, config: dict) -> str:
    """Blind per-task comparison by the frozen judge, using real outcome evidence."""
    directory.mkdir(parents=True, exist_ok=True)
    old_by_id = {r["task_id"]: r for r in old}
    verdicts = []
    for candidate in new:
        baseline = old_by_id[candidate["task_id"]]
        path = directory / (candidate["short_id"] + ".json")
        if path.exists():
            verdicts.append(json.loads(path.read_text())["preference"])
            continue

        def evidence(record):
            session = record.get("session", {})
            trajectory = json.dumps(
                session.get("turns", session.get("messages", session.get("transcript", []))), ensure_ascii=False
            )
            # Full trajectories remain on disk; comparison sees the declared final window and score diagnostics.
            return {
                "score": record["score"],
                "scores": record.get("scores"),
                "timeout": record["timed_out"],
                "tool_errors": record["tool_errors"],
                "grading": record.get("grading_details", record.get("grading")),
                "final_trajectory_window": trajectory[-48000:],
                "trajectory_characters": len(trajectory),
            }

        flip = int(hashlib.sha256(candidate["task_id"].encode()).hexdigest(), 16) % 2
        branches = [evidence(baseline), evidence(candidate)]
        if flip:
            branches.reverse()
        prompt = {"task_id": candidate["task_id"], "A": branches[0], "B": branches[1]}
        body = {
            "model": config.get("judge_model_id") if config.get("external_judge") else config["model_id"],
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "system",
                    "content": "Compare two real executions of the same task. Use measured benchmark "
                    "scores, deliverables in the trajectory, failures and stability. Do not reward longer text. "
                    "Return only JSON with preference: A, B, or tie, and a concise reason. "
                    "Prefer tie without demonstrated improvement.",
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }
        comparison_id = "compare-" + hashlib.sha256(str(directory).encode()).hexdigest()[:24]
        budget = resilient_runner.active_budget(config) if resilient_runner.enabled(config) else None
        timeout = min(1800, max(1, int(budget.require_remaining("outcome-comparator")))) if budget else 1800
        response = httpx.post(
            config["local_api_base_url"].removesuffix("/v1")
            + f"/runs/{comparison_id}"
            + ("/judge" if config.get("external_judge") else "")
            + "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer " + config["local_api_key"]},
            timeout=timeout,
            trust_env=False,
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"]
        content = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        verdict = json.loads(content)
        if verdict.get("preference") not in {"A", "B", "tie"}:
            raise ValueError("Invalid outcome comparator result; candidate remains pending")
        preference = (
            "tie"
            if verdict["preference"] == "tie"
            else ("candidate" if verdict["preference"] == ("A" if flip else "B") else "baseline")
        )
        write_json(
            path,
            {
                "preference": preference,
                "blind_swap": bool(flip),
                "verdict": verdict,
                "baseline_bank": baseline["skill_bank_sha256"],
                "candidate_bank": candidate["skill_bank_sha256"],
            },
        )
        verdicts.append(preference)
    return (
        "candidate"
        if "candidate" in verdicts and "baseline" not in verdicts
        else "baseline"
        if "baseline" in verdicts
        else "tie"
    )


def _alignment_skip(run_dir: Path, number: int, candidate: dict, reason: str, evidence: dict,
                    *, score_available: bool = False) -> None:
    write_json(run_dir / "operations/unattended/skips" / f"alignment-{number:02d}-{candidate['candidate_id']}.json", {
        "kind": "candidate", "phase": "validation", "round": number,
        "candidate_id": candidate["candidate_id"], "name": candidate["name"], "reason": reason,
        "score_available": score_available, "evidence": evidence, "timestamp": now(),
    })


def _resilient_candidate_failure(run_dir: Path, number: int, candidate: dict, error: Exception,
                                 validation: Path | None = None) -> dict:
    """Seal one candidate as unknown while leaving unrelated candidates runnable."""
    candidate_id = str(candidate.get("candidate_id") or canonical_hash(candidate)[:24])
    decision = {"accepted": False, "pending": False, "unknown": True,
        "reason": "candidate_validation_error", "error_type": type(error).__name__, "error": str(error),
        "candidate_id": candidate_id, "name": candidate.get("name"), "timestamp": now()}
    if validation is not None:
        write_json(validation / "decision.json", decision)
    _alignment_skip(run_dir, number, {**candidate, "candidate_id": candidate_id,
                    "name": candidate.get("name", "unknown")},
                    "candidate_validation_error", decision, score_available=False)
    event(run_dir, "candidate_isolated_failure", round=number, **decision)
    return decision


def _verify_skill_dependencies(candidate: dict, bank: Path) -> None:
    contract = candidate.get("dependency_contract")
    if not isinstance(contract, dict) or contract.get("policy") != "parent-bound-cross-skill-files-v1":
        raise ValueError("Missing per-skill parent dependency contract")
    dependencies = contract.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("Invalid per-skill dependencies")
    for item in dependencies:
        skill, relative = item.get("skill"), item.get("path")
        if (not isinstance(skill, str) or not skill or Path(skill).name != skill or skill in {".", ".."}
                or skill == candidate["bundles"][0]["name"] or not isinstance(relative, str) or not relative
                or Path(relative).is_absolute() or ".." in Path(relative).parts):
            raise ValueError("Unsafe or non-cross-skill dependency")
        path = bank / skill / relative
        if (not path.is_file() or not path.resolve().is_relative_to(bank.resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256")):
            raise ValueError(f"Per-skill dependency changed: {skill}/{relative}")


def _merge_skill(working: Path, candidate: dict, previously_accepted: list[dict], validation: Path,
                 config: dict) -> tuple[Path, dict]:
    from scripts.wildclawbench.environment import preflight_candidate_bank

    bundle = candidate["bundles"][0]
    identity = alignment_candidate_id(candidate, config)
    parent_hash = tree_hash(working)
    proposed = validation / "merged-bank"
    building = validation / "merged-bank.building"
    if building.exists():
        shutil.rmtree(building)
    shutil.copytree(working, building)
    target = building / bundle["name"]
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(bundle["bundle_dir"], target)
    if tree_hash(target) != bundle["bundle_sha256"]:
        raise ValueError("Accepted skill bytes changed while merging")
    proposed_hash = tree_hash(building)
    if proposed.exists():
        if tree_hash(proposed) != proposed_hash:
            raise ValueError("Cached per-skill merge bank changed")
        shutil.rmtree(building)
    else:
        os.replace(building, proposed)
    binding = {"scope": "per-skill-v1", "candidate_id": identity,
               "merge_parent_sha256": parent_hash, "merged_bank_sha256": proposed_hash}
    path = validation / "merge-decision.json"
    if path.exists():
        decision = json.loads(path.read_text())
        if any(decision.get(key) != value for key, value in binding.items()):
            raise ValueError("Cached per-skill merge belongs to another parent or candidate")
    else:
        try:
            for accepted in [*previously_accepted, candidate]:
                _verify_skill_dependencies(accepted, proposed)
        except ValueError as error:
            decision = {"accepted": False, "reason": "merged_dependency_changed", "error": str(error)}
        else:
            preflight_candidate = {"candidate_id": "merge-" + identity + "-" + proposed_hash[:12]}
            resilient = resilient_runner.enabled(config)
            preflight = None
            for attempt in range(2) if resilient else range(1):
                attempt_file = validation / f"merge-preflight-attempt-{attempt}.json"
                preflight = json.loads(attempt_file.read_text()) if attempt_file.exists() else None
                if preflight is None:
                    preflight = preflight_candidate_bank(proposed, preflight_candidate, config)
                    if resilient:
                        preflight = {**preflight, "preflight_attempt_index": attempt,
                                     "candidate_bank_sha256": proposed_hash}
                        write_json(attempt_file, preflight)
                if resilient:
                    if (preflight.get("preflight_attempt_index"),
                            preflight.get("candidate_bank_sha256")) != (attempt, proposed_hash):
                        raise ValueError("Cached merge preflight attempt belongs to different content")
                    resilient_runner.observe_infrastructure_result(
                        validation.parents[1] / "infrastructure-circuit.json",
                        f"merge-preflight:{identity}:{attempt}",
                        error_type=preflight.get("error_type"), error=preflight.get("error"))
                    if (resilient_runner.infrastructure_signature(
                            preflight.get("error_type"), preflight.get("error")) is not None
                            and attempt == 0):
                        continue
                break
            infrastructure_unknown = bool(preflight and preflight.get("unknown") is True
                                          and preflight.get("infrastructure_signature"))
            decision = {"accepted": preflight.get("passed") is True,
                        "reason": ("merged" if preflight.get("passed") is True else
                                   "merge_preflight_infrastructure_unknown" if infrastructure_unknown else
                                   "merge_preflight_rejected"),
                        "preflight": preflight}
            if resilient:
                decision["unknown"] = infrastructure_unknown
        decision.update(binding)
        write_json(path, decision)
    if tree_hash(working) != parent_hash or tree_hash(proposed) != proposed_hash:
        raise ValueError("Per-skill merge preflight changed immutable bank bytes")
    return (proposed if decision["accepted"] else working), decision


def _collector_revision(config: dict, directory: Path) -> dict | None:
    """Select a sealed offline collection without changing execution identities."""
    reference = config.get("collector_revision")
    if reference is None:
        return None
    path = Path(reference["path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
        raise ValueError("Collector revision manifest changed")
    revision = json.loads(path.read_text())
    if (revision.get("policy") != "native-collector-revision-v1"
            or revision.get("protocol_hash") != config["protocol_hash"]
            or revision.get("night_dir") != str(directory.resolve())):
        raise ValueError("Collector revision belongs to another protocol or Night")
    for key in ("candidates", "summary"):
        item = revision[key]
        if hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Collector revision artifact changed: " + key)
    return revision


def _prepare_alignment_candidate(candidate: dict, bank: Path, sessions: list[dict], tasks: list[dict],
                                 directory: Path, config: dict, parent_hash: str, per_skill: bool) -> dict:
    """Verify one proposal and materialize its immutable candidate bank."""
    from scripts.wildclawbench.evolution import _history_sources, _normalize_session
    from sharedskill.skill_bundle import read_skill_bundle, read_skill_bundle_with_meta

    if resilient_runner.enabled(config):
        resilient_runner.validate_typed_sources(candidate)
    identity = alignment_candidate_id(candidate, config)
    if (candidate.get("action") != "atomic_changeset" or candidate.get("candidate_id") != identity
            or candidate.get("source_bank_sha256") != parent_hash):
        raise ValueError("Atomic changeset identity or parent changed")
    source_bank = {path.name: read_skill_bundle(path) for path in bank.iterdir()
                   if path.is_dir() and (path / "SKILL.md").is_file()}
    day = [_normalize_session(session, set(source_bank), restrict_to_bank=False) for session in sessions]
    resolved, evidence = _history_sources(candidate["bundles"], source_bank, day)
    if (candidate["source_evidence"] != evidence
            or candidate["source_session_ids"] != sorted(session["source_session_id"] for session in resolved)
            or candidate["source_task_ids"] != sorted({session["task_id"] for session in resolved})):
        raise ValueError("Cached atomic sources differ from real native history/day evidence")
    task_map = {task["task_id"]: task for task in tasks}
    source_ids = candidate["source_task_ids"]
    if not source_ids or len(set(source_ids)) != len(source_ids) or set(source_ids) - task_map.keys():
        raise ValueError("Atomic changeset requires distinct real related tasks")
    related = [task_map[key] for key in sorted(source_ids)]
    if per_skill:
        _verify_skill_dependencies(candidate, bank)
    validation = directory / "validation" / (("skill-" if per_skill else "001-native-night-") + identity)
    validation.mkdir(parents=True, exist_ok=True)
    proposed = validation / "candidate-bank"
    building = validation / "candidate-bank.building"
    if building.exists():
        shutil.rmtree(building)
    shutil.copytree(bank, building)
    names = set()
    for bundle in candidate["bundles"]:
        name = bundle["name"]
        if not name or Path(name).name != name or name in {".", ".."} or name in names:
            raise ValueError("Unsafe or duplicate atomic bundle name")
        names.add(name)
        existing = building / name
        if (bundle["action"] == "create" and existing.exists()
                or bundle["action"] == "refine" and not existing.is_dir()):
            raise ValueError("Atomic bundle action does not match parent bank")
        if bundle["action"] not in {"create", "refine"}:
            raise ValueError("Unsupported atomic bundle action")
        source = Path(bundle["bundle_dir"])
        if (tree_hash(source) != bundle["bundle_sha256"]
                or read_skill_bundle_with_meta(source)[2] != bundle["native_tree_sha256"]):
            raise ValueError("Atomic candidate bundle changed after proposal")
        if existing.exists():
            shutil.rmtree(existing)
        shutil.copytree(source, existing)
    if not names:
        raise ValueError("An atomic changeset cannot be empty")
    proposed_hash = tree_hash(building)
    if proposed.exists():
        if tree_hash(proposed) != proposed_hash:
            raise ValueError("Cached atomic candidate bank changed")
        shutil.rmtree(building)
    else:
        os.replace(building, proposed)
    return {"identity": identity, "source_ids": source_ids, "related": related,
            "validation": validation, "proposed": proposed, "proposed_hash": proposed_hash,
            "names": names}


def _alignment_night(number: int, bank: Path, sessions: list[dict], tasks: list[dict], run_dir: Path,
                     config: dict, directory: Path, candidates: list[dict]) -> Path:
    """Validate native candidates under their frozen atomic or per-skill scope."""
    from scripts.wildclawbench.environment import preflight_candidate_bank
    from scripts.wildclawbench.evolution import _history_sources, _normalize_session

    from sharedskill.skill_bundle import read_skill_bundle, read_skill_bundle_with_meta

    settings = alignment_settings(config)
    per_skill = settings.get("native_candidate_scope") == "per-skill-v1"
    parent_hash = tree_hash(bank)
    if not per_skill and len(candidates) > 1:
        raise ValueError("Alignment expects one native atomic changeset per night")
    if per_skill:
        names = [c["bundles"][0]["name"] for c in candidates if len(c.get("bundles", [])) == 1]
        if len(names) != len(candidates) or len(set(names)) != len(names):
            raise ValueError("Per-skill scope requires one distinct bundle per candidate")
        candidates = sorted(candidates, key=lambda c: c["bundles"][0]["name"])
    collection = _collector_revision(config, directory)
    summary_path = (Path(collection["summary"]["path"]) if collection else
                    directory / "proposals/evolution/proposal-summary.json")
    quarantines = []
    if summary_path.exists():
        quarantines = json.loads(summary_path.read_text()).get("quarantines", [])
        for rejected in quarantines:
            _alignment_skip(run_dir, number, rejected, rejected.get("reason", "unverifiable_sources"), rejected)
    accepted = False
    working = bank
    accepted_candidates, skill_decisions = [], []
    if per_skill:
        skill_decisions.extend({"name": item["name"], "candidate_id": item["candidate_id"],
            "individual_accepted": False, "individual_reason": item["reason"],
            "merge_accepted": False, "merge_reason": "not_run_quarantined"} for item in quarantines)
    for candidate in candidates:
        try:
            prepared = _prepare_alignment_candidate(
                candidate, bank, sessions, tasks, directory, config, parent_hash, per_skill)
        except Exception as error:
            if not resilient_runner.enabled(config):
                raise
            decision = _resilient_candidate_failure(run_dir, number, candidate, error)
            if per_skill:
                skill_decisions.append({"name": candidate.get("name"),
                    "candidate_id": decision["candidate_id"], "individual_accepted": False,
                    "individual_reason": decision["reason"], "merge_accepted": False,
                    "merge_reason": "not_run_candidate_error"})
            continue
        identity, source_ids, related = (prepared[key] for key in ("identity", "source_ids", "related"))
        validation, proposed, proposed_hash = (prepared[key] for key in ("validation", "proposed", "proposed_hash"))
        names = prepared["names"]
        decision_file = validation / "decision.json"
        if decision_file.exists():
            decision = json.loads(decision_file.read_text())
            if (decision.get("changeset_id"), decision.get("parent_sha256"),
                    decision.get("candidate_bank_sha256")) != (identity, parent_hash, proposed_hash):
                raise ValueError("Cached atomic decision belongs to different content")
        else:
            preflight_file = validation / "preflight.json"
            preflight = json.loads(preflight_file.read_text()) if preflight_file.exists() else None
            if preflight is None:
                resilient = resilient_runner.enabled(config)
                if not resilient:
                    preflight = preflight_candidate_bank(proposed, candidate, config)
                    preflight = {**preflight, "candidate_bank_sha256": proposed_hash,
                                 "changeset_id": identity}
                for attempt in range(2) if resilient else ():
                    attempt_file = validation / f"preflight-attempt-{attempt}.json"
                    current = json.loads(attempt_file.read_text()) if attempt_file.exists() else None
                    if current is None:
                        try:
                            current = preflight_candidate_bank(proposed, candidate, config)
                        except (resilient_runner.StageBudgetExceeded,
                                resilient_runner.CircuitBreakerOpen):
                            raise
                        except Exception as error:
                            if not resilient:
                                raise
                            current = {"passed": False, "unknown": True, "reason": "preflight_error",
                                       "error_type": type(error).__name__, "error": str(error)}
                        current = {**current, "candidate_bank_sha256": proposed_hash,
                                   "changeset_id": identity, "preflight_attempt_index": attempt}
                        write_json(attempt_file, current)
                    if ((current.get("candidate_bank_sha256"), current.get("changeset_id"),
                         current.get("preflight_attempt_index")) != (proposed_hash, identity, attempt)):
                        raise ValueError("Cached preflight attempt belongs to different content")
                    if resilient:
                        resilient_runner.observe_infrastructure_result(
                            directory / "infrastructure-circuit.json",
                            f"candidate-preflight:{identity}:{attempt}",
                            error_type=current.get("error_type"), error=current.get("error"))
                    preflight = current
                    signature = resilient_runner.infrastructure_signature(
                        current.get("error_type"), current.get("error"))
                    if not resilient or signature is None or attempt == 1:
                        break
                write_json(preflight_file, preflight)
            if (preflight.get("candidate_bank_sha256"), preflight.get("changeset_id")) != (proposed_hash, identity):
                raise ValueError("Cached preflight belongs to different content")
            if tree_hash(proposed) != proposed_hash:
                raise ValueError("Preflight modified the immutable candidate bank")
            if resilient_runner.enabled(config):
                resilient_runner.observe_infrastructure_result(
                    directory / "infrastructure-circuit.json",
                    f"candidate-preflight:{identity}:{preflight.get('preflight_attempt_index', 'legacy')}",
                    error_type=preflight.get("error_type"), error=preflight.get("error"))
            if preflight.get("passed") is not True:
                decision = {"accepted": False, "reason": preflight.get("reason", "preflight_rejected"),
                            "unknown": preflight.get("unknown", False)}
                _alignment_skip(run_dir, number, candidate, decision["reason"], preflight)
            else:
                validation_id = validation.name
                event(run_dir, "candidate_validation", round=number, candidate_id=validation_id,
                      changeset_id=identity, related_tasks=len(related), bundles=sorted(names))
                resilient = resilient_runner.execution_protocol(config)
                if resilient:
                    stage = Path(run_dir).resolve().parents[1]
                    importer = resilient_runner.baseline_import(config)
                    import_identity = resilient_runner.baseline_execution_identity(
                        config, config["all_tasks"], parent_hash)
                    old, proofs = [], []
                    for task in related:
                        if number == 1:
                            if importer is None:
                                raise resilient_runner.ProtocolError(
                                    "Round-1 validation requires an explicit compatible baseline_import")
                            row, proof = importer.scored_row(task, import_identity, parent_hash)
                        else:
                            row, proof = resilient_runner.same_stage_day_baseline(
                                stage, task, config, parent_hash)
                        old.append(row)
                        proofs.append(proof)
                        write_json(validation / "baseline-reuse" / (task["short_id"] + ".json"), proof)
                    write_json(validation / "baseline-reuse.json", {
                        "policy": "verified-day-baseline-only-v1", "task_count": len(old),
                        "new_agent_executions": 0, "new_scoring_calls": 0,
                        "proof_sha256": canonical_hash(proofs), "tasks": proofs})
                else:
                    old = run_stage(related, bank, run_dir, config, "validation", number, "old", validation_id)
                try:
                    new = run_stage(related, proposed, run_dir, config, "validation", number, "candidate", validation_id)
                    numeric = accept_candidate(old, new, "candidate", config["score_tolerance"])
                    preference = (outcome_preference(old, new, validation / "comparisons", config)
                                  if numeric["accepted"] else "not_run_numeric_gate_failed")
                    decision = accept_candidate(old, new, preference, config["score_tolerance"])
                except (resilient_runner.StageBudgetExceeded, resilient_runner.CircuitBreakerOpen):
                    raise
                except Exception as error:
                    if not resilient:
                        raise
                    decision = _resilient_candidate_failure(run_dir, number, candidate, error)
            if per_skill and decision.get("pending"):
                write_json(validation / "pending-validation.json", {**decision,
                    "changeset_id": identity, "parent_sha256": parent_hash,
                    "candidate_bank_sha256": proposed_hash, "alignment": settings})
                raise RuntimeError("Per-skill scoring incomplete; recover existing executions and snapshots before publication")
            if tree_hash(bank) != parent_hash or tree_hash(proposed) != proposed_hash:
                raise ValueError("Validation modified an immutable atomic bank")
            decision.update(name=candidate["name"], candidate_id=validation.name, changeset_id=identity,
                source_task_ids=source_ids, parent_sha256=parent_hash, candidate_bank_sha256=proposed_hash,
                bundle_tree_sha256=canonical_hash([(b["name"], b["bundle_sha256"]) for b in candidate["bundles"]]),
                bundle_names=sorted(names), alignment=settings, timestamp=now())
            write_json(decision_file, decision)
        if per_skill and decision.get("pending"):
            raise RuntimeError("Cached per-skill scoring is incomplete; do not publish a missing-score rejection")
        accepted = decision["accepted"]
        if accepted:
            if per_skill:
                try:
                    working, merge = _merge_skill(working, candidate, accepted_candidates, validation, config)
                except (resilient_runner.StageBudgetExceeded, resilient_runner.CircuitBreakerOpen):
                    raise
                except Exception as error:
                    if not resilient_runner.enabled(config):
                        raise
                    merge = {"accepted": False, "unknown": True, "reason": "merge_validation_error",
                             "error_type": type(error).__name__, "error": str(error),
                             "candidate_id": identity, "name": candidate["name"]}
                    write_json(validation / "merge-decision.json", merge)
                if merge["accepted"]:
                    accepted_candidates.append(candidate)
                else:
                    _alignment_skip(run_dir, number, candidate, merge["reason"], merge, score_available=True)
                event(run_dir, "candidate_merge_decision", round=number, **merge)
            else:
                working = proposed
        if per_skill:
            merged = any(item["candidate_id"] == identity for item in accepted_candidates)
            skill_decisions.append({"name": candidate["bundles"][0]["name"], "candidate_id": identity,
                "individual_accepted": decision["accepted"], "individual_reason": decision["reason"],
                "merge_accepted": merged, "merge_reason": merge["reason"] if decision["accepted"] else "not_run_individual_rejected",
                "bundle_sha256": candidate["bundles"][0]["bundle_sha256"]})
        event(run_dir, "candidate_decision", round=number, **decision)
    published = run_dir / "banks" / f"S{number}"
    if tree_hash(bank) != parent_hash:
        raise ValueError("The immutable parent changed during the night")
    temporary = published.with_name(published.name + ".publishing")
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(working, temporary)
    bank_hash = tree_hash(temporary)
    if published.exists():
        if tree_hash(published) != bank_hash:
            raise ValueError("Refusing to replace a different published atomic bank")
        shutil.rmtree(temporary)
    else:
        os.replace(temporary, published)
    publication = {"round": number, "parent_sha256": parent_hash, "bank_sha256": bank_hash,
                   "timestamp": now(), "candidate_count": len(candidates), "alignment": settings,
                   "changeset_accepted": accepted}
    if collection:
        publication["collector_revision"] = config["collector_revision"]
    if config.get("source_amendment"):
        publication["source_amendment"] = config["source_amendment"]
    if per_skill:
        publication.update(changeset_accepted=bool(accepted_candidates), validation_scope="per-skill-v1",
            validation_parent_sha256=parent_hash, merge_order="skill_name_ascending",
            accepted_skill_names=[item["bundles"][0]["name"] for item in accepted_candidates],
            rejected_skill_names=sorted({item["name"] for item in skill_decisions if not item["merge_accepted"]}),
            accepted_candidate_ids=[item["candidate_id"] for item in accepted_candidates],
            per_skill_decisions=skill_decisions, merge_performed=bool(accepted_candidates),
            bank_changed=bank_hash != parent_hash,
            merged_skill_bundle_hashes=[{"name": item["bundles"][0]["name"], "candidate_id": item["candidate_id"],
                "bundle_sha256": item["bundles"][0]["bundle_sha256"]} for item in accepted_candidates])
    write_json(directory / "publication.json", publication)
    event(run_dir, "night_published", **publication)
    return published


def night(number: int, bank: Path, sessions: list[dict], tasks: list[dict], run_dir: Path, config: dict) -> Path:
    from scripts.wildclawbench.evolution import propose_candidates

    directory = run_dir / "nights" / f"{number:02d}"
    directory.mkdir(parents=True, exist_ok=True)
    published = run_dir / "banks" / f"S{number}"
    publication_file = directory / "publication.json"
    collection = _collector_revision(config, directory)
    if publication_file.exists():
        publication = json.loads(publication_file.read_text())
        if collection and publication.get("collector_revision") != config["collector_revision"]:
            raise ValueError("Published night belongs to another collector revision")
        if alignment_settings(config) and publication.get("alignment") != alignment_settings(config):
            raise ValueError("Published night belongs to another alignment protocol")
        if tree_hash(published) != publication["bank_sha256"] or tree_hash(bank) != publication["parent_sha256"]:
            raise RuntimeError("Published bank or parent hash changed")
        return published
    candidates_file = (Path(collection["candidates"]["path"]) if collection else
                       directory / "candidates.json")
    if candidates_file.exists():
        candidates = json.loads(candidates_file.read_text())
    else:
        night_id = "night-" + hashlib.sha256(str(directory).encode()).hexdigest()[:24]
        night_api = config["local_api_base_url"].removesuffix("/v1") + f"/runs/{night_id}/v1"
        candidates = propose_candidates(
            directory / "proposals",
            bank,
            sessions,
            {**config, "llm_base_url": night_api, "local_api_base_url": night_api},
        )
        write_json(candidates_file, candidates)
    if alignment_settings(config):
        return _alignment_night(number, bank, sessions, tasks, run_dir, config, directory, candidates)
    working = directory / "working-bank"
    if working.exists():
        shutil.rmtree(working)
    shutil.copytree(bank, working)
    task_map = {task["task_id"]: task for task in tasks}
    for index, candidate in enumerate(candidates):
        name = candidate["name"]
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError("Unsafe candidate skill name")
        if not (working / name).exists() and name in config.get("environment_skill_names", []):
            raise ValueError("A new shared skill may not shadow a frozen image skill")
        if candidate.get("action") == "create" and (working / name).exists():
            raise ValueError("A create proposal collides with an existing shared skill")
        candidate_id = f"{index + 1:03d}-{name}"
        validation = directory / "validation" / candidate_id
        validation.mkdir(parents=True, exist_ok=True)
        related_ids = candidate["source_task_ids"]
        if not related_ids or any(t not in task_map for t in related_ids):
            raise ValueError("Candidate must reference real training tasks")
        related = [task_map[t] for t in sorted(set(related_ids))]
        candidate_bank = validation / "candidate-bank"
        if candidate_bank.exists():
            shutil.rmtree(candidate_bank)
        shutil.copytree(working, candidate_bank)
        bundle = Path(candidate["bundle_dir"])
        from sharedskill.skill_bundle import read_skill_bundle_with_meta

        # Keep an independent experiment tree hash, including every copied file.
        bundle_hash = tree_hash(bundle)
        if bundle_hash != candidate["bundle_sha256"]:
            raise ValueError("Candidate bundle changed after proposal")
        if (
            candidate.get("native_tree_sha256")
            and read_skill_bundle_with_meta(bundle)[2] != candidate["native_tree_sha256"]
        ):
            raise ValueError("Native candidate bundle changed after proposal")
        existing = candidate_bank / name
        if existing.exists():
            shutil.rmtree(existing)
        shutil.copytree(bundle, existing)
        parent_hash = tree_hash(working)
        proposed_hash = tree_hash(candidate_bank)
        decision_file = validation / "decision.json"
        if decision_file.exists():
            decision = json.loads(decision_file.read_text())
            if (decision["parent_sha256"], decision["candidate_bank_sha256"], decision["bundle_tree_sha256"]) != (
                parent_hash,
                proposed_hash,
                bundle_hash,
            ):
                raise ValueError("Candidate parent/content changed after validation")
        else:
            event(run_dir, "candidate_validation", round=number, candidate_id=candidate_id, related_tasks=len(related))
            old = run_stage(related, working, run_dir, config, "validation", number, "old", candidate_id)
            new = run_stage(related, candidate_bank, run_dir, config, "validation", number, "candidate", candidate_id)
            numeric_gate = accept_candidate(old, new, "candidate", config["score_tolerance"])
            preference = (
                outcome_preference(old, new, validation / "comparisons", config)
                if numeric_gate["accepted"]
                else "not_run_numeric_gate_failed"
            )
            decision = {
                **accept_candidate(old, new, preference, config["score_tolerance"]),
                "name": name,
                "candidate_id": candidate_id,
                "source_task_ids": related_ids,
                "parent_sha256": parent_hash,
                "candidate_bank_sha256": proposed_hash,
                "bundle_tree_sha256": bundle_hash,
                "timestamp": now(),
            }
            write_json(decision_file, decision)
        if decision["accepted"]:
            target = working / name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(bundle, target)
            if tree_hash(working) != proposed_hash:
                raise RuntimeError("Promoted bytes differ from the validated bank")
        event(
            run_dir,
            "candidate_decision",
            round=number,
            candidate_id=candidate_id,
            accepted=decision["accepted"],
            reason=decision["reason"],
        )
    temporary = published.with_name(published.name + ".publishing")
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(working, temporary)
    bank_hash = tree_hash(temporary)
    if published.exists():
        if tree_hash(published) != bank_hash:
            raise RuntimeError("Existing publication has different bytes")
        shutil.rmtree(temporary)
    else:
        os.replace(temporary, published)
    write_json(
        publication_file,
        {
            "round": number,
            "parent_sha256": tree_hash(bank),
            "bank_sha256": bank_hash,
            "timestamp": now(),
            "candidate_count": len(candidates),
        },
    )
    event(run_dir, "night_published", round=number, bank_sha256=bank_hash, candidates=len(candidates))
    return published


def pilot(tasks: list[dict], bank: Path, run_dir: Path, config: dict):
    check_services(config)
    from scripts.wildclawbench.interface_checks import verify_interfaces

    verify_interfaces(config, run_dir / "interfaces")
    update_state(run_dir, "pilot")
    selected = [t for t in tasks if t["short_id"] in config["pilot_short_ids"]]
    if not selected:
        raise ValueError("Pilot task IDs must include tasks from this experiment's selected manifest")
    # Pilot concurrency matches the main trial; its identities do not change the fixed main assignment.
    selected = [{**task, "user_id": f"user-{i + 1:02d}"} for i, task in enumerate(selected)]
    start = time.monotonic()
    result = run_stage(selected, bank, run_dir, config, "pilot", 0)
    elapsed = time.monotonic() - start
    total_seconds = sum(r["wall_seconds"] for r in result)
    write_json(
        run_dir / "pilot-gate.json",
        {
            "passed": True,
            "tasks": len(result),
            "timestamp": now(),
            "wall_seconds": elapsed,
            "task_seconds": total_seconds,
            "fixed_execution_count": fixed_execution_count(tasks, config),
            "extrapolated_fixed_wall_hours": elapsed * fixed_execution_count(tasks, config) / len(result) / 3600,
            "limitations": "Selected pilot tasks; excludes all night proposal/A-B overhead. "
            "GPU contention prevents linear scaling.",
        },
    )
    event(
        run_dir,
        "pilot_finished",
        tasks=len(result),
        seconds=elapsed,
        extrapolated_fixed_hours=elapsed * fixed_execution_count(tasks, config) / len(result) / 3600,
    )
    update_state(run_dir, "pilot_complete")
    generate(run_dir, tasks)


def run_experiment(tasks: list[dict], initial: Path, run_dir: Path, config: dict):
    check_services(config)
    gate = run_dir / "pilot-gate.json"
    if not gate.exists() or not json.loads(gate.read_text()).get("passed"):
        pilot(tasks, initial, run_dir, config)
    bank = initial
    rounds = int(config.get("rounds", config.get("days", 6)))
    for number in range(1, rounds + 1):
        update_state(run_dir, "day", round=number, bank_sha256=tree_hash(bank))
        results = run_stage(tasks, bank, run_dir, config, "day", number)
        update_state(run_dir, "night", round=number)
        # Only this day's real sessions become evolution evidence.
        bank = night(number, bank, [r["session"] for r in results], tasks, run_dir, config)
        generate(run_dir, tasks)
    update_state(run_dir, "endpoint")

    # Pair arms within each task; randomized first arm reduces systematic endpoint timing bias.
    def endpoint_worker(user_tasks):
        for task in user_tasks:
            arms = [("S0", initial), (f"S{rounds}", bank)]
            random.Random(config["seed"] + int(task["short_id"].replace(".", ""))).shuffle(arms)
            for arm, selected_bank in arms:
                result = run_one(task, selected_bank, run_dir, config, "endpoint", rounds, arm)
                if result["status"] != "completed" or result["score"] is None:
                    raise RuntimeError("Endpoint pair needs infrastructure/score repair")

    assignments = [[t for t in tasks if t["user_id"] == f"user-{i + 1:02d}"] for i in range(config["users"])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=config["users"]) as pool:
        for future in concurrent.futures.as_completed([pool.submit(endpoint_worker, group) for group in assignments]):
            future.result()
    update_state(run_dir, "complete", completed_at=now())
    generate(run_dir, tasks)
    event(run_dir, "experiment_complete")


def readiness(config: dict, run_id: str | None = None) -> dict:
    """Inspect configuration and local assets without network or model calls."""
    from scripts.wildclawbench import environment

    paths = environment.paths(config)
    missing = []
    try:
        tasks = discover_tasks(config)
    except (RuntimeError, OSError, ValueError) as error:
        tasks = []
        missing.append(str(error))
    return {
        "ready": not missing,
        "mode": "offline_readiness",
        "rounds": config["rounds"],
        "users": config["users"],
        "endpoint_snapshot": config["endpoint_snapshot"],
        "expected_task_count": len(tasks) or config.get("expected_task_count", 60),
        "fixed_execution_count": fixed_execution_count(tasks, config) if tasks else None,
        "run_id": run_id,
        "artifact_run_dir": str(Path(config["artifacts_dir"]) / run_id) if run_id else None,
        "output_run_dir": str(Path(config["outputs_dir"]) / run_id) if run_id else None,
        "runtime_dir": str(paths["runtime"]),
        "upstream": str(paths["upstream"]),
        "missing": missing,
        "services_checked": False,
        "model_calls": 0,
    }


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="run",
                        choices=["prepare", "pilot", "run", "resume", "report", "status", "readiness"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="Inspect readiness without starting services or tasks")
    arguments = parser.parse_args(argv)
    if arguments.run_id and (not arguments.run_id.replace("-", "").replace("_", "").replace(".", "").isalnum()
                             or arguments.run_id in {".", ".."}):
        parser.error("--run-id must be a plain identifier without path separators")
    config = load_config(arguments.config)
    if arguments.dry_run or arguments.command == "readiness":
        print(json.dumps(readiness(config, arguments.run_id), ensure_ascii=False, indent=2))
        return
    if config.get("read_only") or config.get("historical"):
        if arguments.command not in {"status", "report"}:
            raise ValueError("Historical/read-only configurations cannot launch or resume work")
    runtime = Path(config["runtime_dir"])
    if arguments.command == "prepare":
        from scripts.wildclawbench.environment import prepare, prepare_sources
        from scripts.wildclawbench.provenance import prepare_input_manifest

        prepare_sources(config)
        tasks = discover_tasks(config)
        initial = runtime / "prepared/S0"
        bank_hash = create_initial_bank(config, initial)
        preparation = prepare(config)
        if preparation.get("status") == "ready":
            tasks = prepare_input_manifest(config, tasks)
        write_json(runtime / "prepared/task-manifest.json", tasks)
        preparation["initial_bank_sha256"] = bank_hash
        write_json(runtime / "prepared/readiness.json", preparation)
        print(json.dumps(preparation, ensure_ascii=False, indent=2))
        return
    if arguments.run_dir is None and not arguments.run_id:
        parser.error("--run-id is required for a concrete run")
    run_dir = (arguments.run_dir or Path(config["artifacts_dir"]) / arguments.run_id).resolve()
    if arguments.run_dir is None:
        config["artifact_run_dir"] = str(run_dir)
        config["output_run_dir"] = str(Path(config["outputs_dir"]) / arguments.run_id)
        config.setdefault("gateway_log_dir", str(Path(config["output_run_dir"]) / "api-evidence"))
    if arguments.command == "status":
        state_path = run_dir / "state.json"
        status = json.loads(state_path.read_text()) if state_path.exists() else {"status": "not_started"}
        status.update(run_dir=str(run_dir), output_run_dir=str(output_root(run_dir, config)),
                      rounds=config["rounds"], endpoint_snapshot=config["endpoint_snapshot"])
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    if arguments.command == "report":
        frozen = run_dir / "frozen-protocol.json"
        if not frozen.is_file():
            raise ValueError("A frozen run is required for reporting")
        tasks = json.loads(frozen.read_text())["tasks"]
        print(json.dumps(generate(run_dir, tasks), ensure_ascii=False, indent=2))
        return
    tasks = discover_tasks(config)
    from scripts.wildclawbench.provenance import prepare_input_manifest

    tasks = prepare_input_manifest(config, tasks)
    with exclusive_run(run_dir):
        output_root(run_dir, config).mkdir(parents=True, exist_ok=True)
        RUN_OUTPUTS[run_dir.resolve()] = output_root(run_dir, config)
        config["protocol_hash"] = freeze(config, tasks, run_dir)
        config["all_tasks"] = tasks
        latest = runtime / "latest-run.txt"
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(str(run_dir) + "\n")
        previous = run_dir / "state.json"
        if previous.exists() and json.loads(previous.read_text()).get("status") == "complete":
            print(json.dumps({"status": "complete", "reused": True, "run_dir": str(run_dir)}))
            return
        initial = run_dir / "banks/S0"
        create_initial_bank(config, initial)
        try:
            if arguments.command == "pilot":
                pilot(tasks, initial, run_dir, config)
            else:
                run_experiment(tasks, initial, run_dir, config)
        except Exception as error:
            update_state(run_dir, "needs_attention", error=f"{type(error).__name__}: {error}")
            event(run_dir, "needs_attention", error=f"{type(error).__name__}: {error}")
            generate(run_dir, tasks)
            raise


if __name__ == "__main__":
    main()
