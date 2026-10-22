#!/usr/bin/env python3
"""Run configured clean/poisoned Day/Night cycles with deployed-bank continuity."""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
from contextlib import nullcontext
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

from scripts.injection import e2e_30 as cohort

from scripts.injection.cohort_injection import inject_uploaded
from scripts.injection import cohort_sandbox
from scripts.injection import resilient_runner


e2e = cohort.e2e
ALL_CONDITIONS = ("clean", "poisoned")
CONDITIONS = ALL_CONDITIONS
ROUNDS = 3
MAX_DAY_ATTEMPTS = 2
ROOT = cohort.ROOT
EXPERIMENT = cohort.EXPERIMENT


def configured_rounds(config: dict) -> int:
    settings = config.get("settings", config)
    number = config.get("rounds", settings.get("rounds", settings.get("days", ROUNDS)))
    if type(number) is not int or number < 1:
        raise ValueError("A positive integer round count is required")
    return number


def configured_conditions(config: dict) -> tuple[str, ...]:
    settings = config.get("settings", config)
    values = tuple(config.get("conditions", settings.get("conditions", ALL_CONDITIONS)))
    if not values or len(set(values)) != len(values) or any(value not in ALL_CONDITIONS for value in values):
        raise ValueError("conditions must contain one or both unique clean/poisoned branches")
    return values


def configure(config_file: Path) -> dict:
    global EXPERIMENT, ROUNDS, CONDITIONS
    settings = cohort.configure(config_file)
    EXPERIMENT, ROUNDS = cohort.EXPERIMENT, settings["rounds"]
    CONDITIONS = configured_conditions(settings)
    resilient_runner.execution_protocol(settings)
    return settings


def stop_gateway(process) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)


def _gateway_port_state(port: int) -> str:
    """Match the frozen no-reuse bind, distinguishing listeners from teardown."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
        else:
            return "available"
    states = set()
    for name in ("tcp", "tcp6"):
        path = Path("/proc/net") / name
        try:
            lines = path.read_text().splitlines()[1:]
        except FileNotFoundError:
            if name == "tcp6":
                continue
            raise RuntimeError("Cannot inspect gateway TCP listeners") from None
        for line in lines:
            fields = line.split()
            if len(fields) >= 4 and int(fields[1].rsplit(":", 1)[1], 16) == port:
                states.add(fields[3])
    if "0A" in states:
        return "listener"
    return "time_wait" if "06" in states else "busy_without_listener"


def start_gateway(run_dir: Path, settings: dict, engine: Path, *, port_wait_seconds: float = 90):
    """Wait for a non-listening socket to clear before the frozen startup check."""
    if resilient_runner.enabled(settings):
        budget = resilient_runner.active_budget(settings)
        if budget is not None:
            port_wait_seconds = min(port_wait_seconds, budget.require_remaining("night-gateway-port"))
    port = int(settings.get("alignment_gateway_port", 19192))
    started = time.monotonic()
    deadline = started + port_wait_seconds
    waiting = False

    def record(event, state):
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "gateway-port-wait.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": e2e.now(), "event": event, "port": port, "socket_state": state,
                "elapsed_seconds": time.monotonic() - started, "maximum_wait_seconds": port_wait_seconds}) + "\n")

    while True:
        state = _gateway_port_state(port)
        if state == "listener":
            record("refused_listener", state)
            raise RuntimeError(f"Alignment gateway port {port} has a live listener; refusing to use or replace it")
        if state == "available":
            if waiting:
                record("wait_complete", state)
            return e2e.start_gateway(run_dir, settings, engine)
        if not waiting:
            record("wait_started", state)
            waiting = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            record("wait_timed_out", state)
            raise RuntimeError(f"Alignment gateway port {port} remained busy without a listener "
                               f"after {port_wait_seconds:g}s; no gateway or Agent was started")
        time.sleep(min(1, remaining))


def copy_bank(source: Path, target: Path, tree_hash) -> str:
    expected = tree_hash(source)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    if tree_hash(target) != expected:
        raise ValueError(f"Deployed bank continuity differs: {source} -> {target}")
    return expected


def catalog_hash(bank: Path) -> str:
    from scripts.wildclawbench.alignment_catalog import inspect_bank, render_catalog
    catalog, _mode = render_catalog(inspect_bank(bank)["skills"])
    return e2e.sha256(catalog.encode())


def sources() -> list[dict]:
    # Freeze the actual native dependency trees as well as orchestration code.
    _, engine, *_ = e2e.load_components()
    paths = set(Path(__file__).parent.glob("*.py"))
    paths.update(engine.glob("*.py"))
    for directory in (ROOT / "sharedskill", ROOT / "evolve_server"):
        paths.update(directory.rglob("*.py"))
        paths.update(directory.rglob("*.md"))
    return [{"path": str(path.resolve()), "sha256": e2e.sha256(path.read_bytes())}
            for path in sorted(paths)]


def prepare_bootstrap(source: Path, target: Path) -> Path:
    """Import intact Day evidence; recollect only tasks with lost native uploads."""
    source, target = source.resolve(), target.resolve()
    settings = e2e.read_json(source / "config.runtime.json")
    *_, tree_hash, _ = e2e.load_components()
    cohort.validate_resume(source, settings, tree_hash)
    if target.exists():
        prior = e2e.read_json(target / "source-import.json", {})
        if prior.get("source_run") != str(source):
            raise ValueError("Bootstrap target is not an authenticated import of this run")
        return target
    tasks = settings["all_tasks"]
    captured = [cohort.load_completed_day(source, task)[1] for task in tasks]
    cohort.finalize_day_sessions(source, tasks, captured, tree_hash(source / "banks/S0"))
    index = e2e.read_json(source / "finalized-day-inputs/manifest.json")
    missing = [item["short_id"] for item in index["tasks"] if not item["input_coverage_complete"]]
    target.mkdir(parents=True)
    copy_bank(source / "banks/S0", target / "banks/S0", tree_hash)
    imports = []
    for task in tasks:
        short = task["short_id"]
        if short in missing:
            continue
        original = source / "day/tasks" / short
        state = e2e.read_json(original / "state.json")
        output, service = cohort.day_paths(source, task, state)
        destination = target / "day/tasks" / short
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(original, destination)
        destination = target / service.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(service, destination)
        imports.append({"task": short, "source_output": str(output), "source_service": str(service),
            "score": state["task_score"], "execution_sha256": e2e.sha256((output / "execution.json").read_bytes()),
            "task_state_sha256": e2e.sha256((original / "state.json").read_bytes()),
            "protocol_hash": settings["protocol_hash"], "execution_guard": "legacy_native_proxy"})
    settings.update(runtime_dir=str(target / "runtime"), gateway_log_dir=str(target / "runtime/api-evidence"),
        cohort_day_alignment=True, bootstrap_import_source=str(source), trajectory_recollection_task_ids=missing)
    settings["protocol_hash"] = cohort.digest({"parent_protocol": settings["protocol_hash"],
        "supplement": "missing-native-upload-recollection-aligned-v1", "recollect_tasks": missing,
        "orchestration": sources()})
    e2e.write_json(target / "config.runtime.json", settings)
    e2e.write_json(target / "day/task/shared-skill-names.json",
                   e2e.read_json(source / "day/task/shared-skill-names.json"))
    e2e.write_json(target / "source-import.json", {"source_run": str(source), "created_at": e2e.now(),
        "imported_tasks": imports, "fresh_recollection_tasks": missing,
        "prior_scores_preserved_in_source_run": True,
        "original_complete_day_mean": e2e.read_json(source / "day/state.json")["mean_score"],
        "mixed_day1_collection_protocols_explicit": True,
        "new_execution_guard": "bound_agent_catalog_and_quiescent_snapshot",
        "old_executions_are_not_regraded_or_overwritten": True})
    return target


def prepare(run_id: str, source_run: str | None, bootstrap_day_run: str | None, resume: bool) -> tuple[Path, dict]:
    e2e.require_execution()
    run = e2e.run_directory(run_id)
    _, _, _, _, _, _, _, tree_hash, _ = e2e.load_components()
    if resume:
        config = e2e.read_json(run / "cycles-config.json")
        if (not config or config["sources"] != sources() or config.get("rounds") != ROUNDS
                or config.get("native_llm_request_timeout_seconds") != 600
                or config.get("conditions") != list(CONDITIONS)):
            raise ValueError("Cycle orchestration sources differ from the frozen run")
    else:
        if not source_run:
            raise ValueError("A frozen original --source-run is required")
        source = Path(source_run).resolve()
        baseline = Path(bootstrap_day_run or source_run).resolve()
        source_settings = e2e.read_json(source / "config.runtime.json")
        cohort.validate_resume(source, source_settings, tree_hash)
        settings = copy.deepcopy(source_settings)
        requested = e2e.load_settings()
        for key in ("execution_protocol", "stage_budgets", "baseline_import", "conditions",
                    "agent_prompt_sha256", "runtime_contract_sha256", "task_manifest_sha256"):
            if key in requested:
                settings[key] = copy.deepcopy(requested[key])
        protocol = resilient_runner.execution_protocol(settings)
        tasks = settings["all_tasks"]
        importer = resilient_runner.baseline_import(settings)
        if protocol is not None and importer is None:
            raise resilient_runner.ProtocolError(
                "resilient-v1 requires an explicit compatible baseline_import; fresh fallback is forbidden")
        if importer is not None:
            identity = resilient_runner.baseline_execution_identity(
                settings, tasks, tree_hash(source / "banks/S0"))
            importer.sessions(tasks, identity)
            bootstrap_manifest_sha256 = e2e.sha256(importer.manifest_path.read_bytes())
        else:
            baseline_config = e2e.read_json(baseline / "config.runtime.json")
            if ([task["task_id"] for task in baseline_config["all_tasks"]] != [task["task_id"] for task in tasks]
                    or tree_hash(baseline / "banks/S0") != tree_hash(source / "banks/S0")):
                raise ValueError("Bootstrap Day differs from the original task cohort or S0")
            day = e2e.read_json(baseline / "day/state.json", {})
            frozen = e2e.read_json(baseline / "finalized-day-inputs/manifest.json", {})
            if day.get("status") != "completed" or day.get("scored_tasks") != 30 or frozen.get("task_count") != 30:
                raise ValueError("Bootstrap requires all 30 real scored tasks and finalized native uploads")
            bootstrap_manifest_sha256 = e2e.sha256((baseline / "finalized-day-inputs/manifest.json").read_bytes())
        run.mkdir(parents=True, exist_ok=False)
        payload_bytes = Path(settings["payload_file"]).read_bytes()
        frozen_payload = run / "frozen-payload.json"
        frozen_payload.write_bytes(payload_bytes)
        config = {"run_id": run_id, "source_run": str(source), "bootstrap_day_run": str(baseline),
            "initial_bank": str(source / "banks/S0"),
            "source_protocol_hash": settings["protocol_hash"], "settings": settings, "sources": sources(),
            "bootstrap_manifest_sha256": bootstrap_manifest_sha256,
            "conditions": list(configured_conditions(settings)), "rounds": ROUNDS, "cohort_day_alignment": True,
            "native_llm_request_timeout_seconds": 600,
            "frozen_payload": str(frozen_payload), "payload_sha256": e2e.sha256(payload_bytes),
            "poisoning": "Repeated exposure: one verified carrier per selected task on each poisoned Night",
            "sandbox_schedule": f"Only final S{ROUNDS} deployed banks; selected unprotected tasks per condition",
            "paid_api_budget_usd": 0, "created_at": e2e.now()}
        e2e.write_json(run / "cycles-config.json", config)
        for index, record in enumerate(config["sources"]):
            destination = run / "frozen-orchestrator" / f"{index:02d}-{Path(record['path']).name}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(record["path"], destination)
    baseline = Path(config["bootstrap_day_run"])
    if e2e.sha256(Path(config["frozen_payload"]).read_bytes()) != config["payload_sha256"]:
        raise ValueError("Frozen poisoning payload changed")
    importer = resilient_runner.baseline_import(config["settings"])
    manifest_path = importer.manifest_path if importer is not None else baseline / "finalized-day-inputs/manifest.json"
    if e2e.sha256(manifest_path.read_bytes()) != config["bootstrap_manifest_sha256"]:
        raise ValueError("Bootstrap native-input manifest changed")
    return run, config


def wait_bootstrap(run_id: str, baseline: Path, worker_pid: int) -> None:
    """Wait only on the recorded collection worker, then freeze its real uploads."""
    record = e2e.read_json(baseline / "day-workers/manual-launch.json", {})
    if record.get("pid") != worker_pid:
        raise ValueError("Bootstrap worker identity does not match its launch record")
    marker = EXPERIMENT / "runtime/launches" / f"{run_id}-bootstrap-wait.json"
    while True:
        current = cohort.process_identity(worker_pid)
        if not current or current[0] != record["start_ticks"] or current[1] == "Z":
            break
        e2e.write_json(marker, {"status": "waiting_for_bootstrap", "pid": os.getpid(),
            "worker_pid": worker_pid, "updated_at": e2e.now(),
            "progress": e2e.read_json(baseline / "day/progress.json", {})})
        time.sleep(15)
    if e2e.read_json(baseline / "day/state.json", {}).get("status") != "completed":
        raise RuntimeError("Bootstrap collection remains incomplete; no Night was started")
    settings = e2e.read_json(baseline / "config.runtime.json")
    *_, tree_hash, _ = e2e.load_components()
    captured = [cohort.load_completed_day(baseline, task)[1] for task in settings["all_tasks"]]
    cohort.finalize_day_sessions(baseline, settings["all_tasks"], captured, tree_hash(baseline / "banks/S0"))
    if not e2e.read_json(baseline / "finalized-day-inputs/manifest.json")["input_coverage_complete"]:
        raise RuntimeError("Bootstrap still has missing authentic uploads; preserving the gap")
    e2e.write_json(marker, {"status": "bootstrap_verified", "pid": os.getpid(),
        "worker_pid": worker_pid, "updated_at": e2e.now(), "native_input_coverage_complete": True})


def setup_stage(run: Path, config: dict, condition: str, number: int, parent_bank: Path, tree_hash) -> tuple[Path, dict]:
    from scripts.injection.cycle_revision import stage_root
    stage = stage_root(run, config) / "rounds" / f"{number:02d}" / condition
    bank_hash = copy_bank(parent_bank, stage / "banks/S0", tree_hash)
    catalog = catalog_hash(stage / "banks/S0")
    settings = copy.deepcopy(config["settings"])
    settings.update(experiment_root=str(EXPERIMENT), runtime_dir=str(stage / "runtime"),
        gateway_log_dir=str(stage / "runtime/api-evidence"), cohort_day_alignment=True,
        native_llm_request_timeout_seconds=config["native_llm_request_timeout_seconds"],
        cycle_round=number, cycle_condition=condition)
    settings["protocol_hash"] = cohort.digest({"source_protocol_hash": config["source_protocol_hash"],
        "cycle_sources": config["sources"], "run_id": config["run_id"], "round": number,
        "method_revision": config.get("method_revision"),
        "validation_binding_sha256": config.get("validation_binding_sha256"),
        "source_evidence_revision": settings.get("alignment", {}).get("source_evidence_revision"),
        "condition": condition, "parent_bank_sha256": bank_hash, "catalog_sha256": catalog,
        "payload_sha256": config["payload_sha256"],
        "execution_protocol": settings.get("execution_protocol"),
        "cohort_day_alignment": True, "bootstrap_manifest_sha256": config["bootstrap_manifest_sha256"]})
    settings["local_api_base_url"] = settings["llm_base_url"] = f"http://127.0.0.1:{settings['alignment_gateway_port']}/v1"
    if number > 1:
        day_budget = resilient_runner.budget_from_config(settings, "day", stage)
        if day_budget is not None:
            settings["stage_budget"] = {"protocol_version": resilient_runner.VERSION, "stage": day_budget.stage,
                "path": str(day_budget.path.resolve()), "deadline_epoch": day_budget.deadline_epoch,
                "limit_seconds": day_budget.limit_seconds}
    path = stage / "config.runtime.json"
    prior = e2e.read_json(path)
    if prior and prior != settings:
        raise ValueError(f"Frozen cycle stage configuration changed: {number}/{condition}")
    if not prior:
        e2e.write_json(path, settings)
        e2e.write_json(stage / "stage-binding.json", {"round": number, "condition": condition,
            "parent_bank": str(parent_bank), "bank_sha256": bank_hash, "catalog_sha256": catalog,
            "protocol_hash": settings["protocol_hash"], "fresh_day_required": number > 1})
        names = sorted(p.name for p in (stage / "banks/S0").iterdir() if (p / "SKILL.md").is_file())
        e2e.write_json(stage / "day/task/shared-skill-names.json", names)
    return stage, settings


def day_worker(stage: Path) -> None:
    """A real process boundary makes post-shutdown native uploads immutable."""
    from scripts.injection.cohort_aligned_api import start_aligned_api  # Fail closed if the aligned bridge is unavailable.
    settings = e2e.read_json(stage / "config.runtime.json")
    os.environ["SHAREDSKILL_LLM_REQUEST_TIMEOUT_S"] = str(settings.get("native_llm_request_timeout_seconds", 600))
    if settings.get("cohort_day_alignment") is not True:
        raise ValueError("Fresh cycle Day requires the bound API/snapshot execution chain")
    _, engine, _, _, controller, task_runtime, _, tree_hash, _ = e2e.load_components()
    process = None
    try:
        process, gateway, _ = start_gateway(stage, settings, engine)
        from scripts.injection.cycle_validation import validation_adapter
        context = (validation_adapter(controller, settings["cohort_validation_binding"])
                   if settings.get("method_revision") else nullcontext())
        with context:
            cohort.run_day_cohort(stage, settings, settings["all_tasks"], task_runtime, gateway, tree_hash)
    finally:
        stop_gateway(process)


def infrastructure_recoverable(stage: Path, tasks: list[dict]) -> bool:
    """Allow one transport/capture/score retry, never a measured low-score retry."""
    for task in tasks:
        state = e2e.read_json(stage / "day/tasks" / task["short_id"] / "state.json", {})
        if state.get("status") == "completed":
            continue
        output, _service = cohort.day_paths(stage, task, state)
        if (output / "execution.json").exists():
            continue  # Existing executor restores scoring; it does not run the Agent again.
        result = e2e.read_json(output / "runtime-result.json", {})
        text = str(result.get("error") or state.get("error") or "").lower()
        if result.get("status") != "infrastructure_error" or not any(
                marker in text for marker in ("connection", "transport", "503", "502", "runtime child exited")):
            return False
    return True


def stop_preserved_grading_containers(stage: Path, settings: dict) -> None:
    """Stop only owned, quiescent executions whose immutable grade snapshot exists."""
    _, _, _, _, _, runtime, *_ = e2e.load_components()
    for task in settings["all_tasks"]:
        state = e2e.read_json(stage / "day/tasks" / task["short_id"] / "state.json", {})
        if state.get("status") == "completed":
            continue
        output, _ = cohort.day_paths(stage, task, state)
        execution = e2e.read_json(output / "execution.json", {})
        name = execution.get("container", "")
        if not name.startswith("sharedskill-task-"):
            continue
        probe = runtime._docker(["inspect", name], settings, check=False)
        if probe.returncode:
            continue
        container = json.loads(probe.stdout)[0]
        if not container.get("State", {}).get("Running"):
            continue
        run_id = "wcb-" + hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:20]
        quiet = e2e.read_json(output / "agent-quiescence.json", {})
        if (container.get("Config", {}).get("Labels", {}).get("org.sharedskill.run_id") != run_id
                or quiet.get("quiescent") is not True or quiet.get("surviving_writers")
                or not execution.get("output_snapshot_sha256")
                or not (output / "grading-recovery-manifest.json").exists()):
            raise RuntimeError("Preserved grading container lacks verified ownership and frozen recovery evidence")
        runtime._docker(["stop", name], settings)


def run_day(stage: Path, settings: dict) -> None:
    tasks = settings["all_tasks"]
    for attempt in range(1, MAX_DAY_ATTEMPTS + 1):
        if e2e.read_json(stage / "day/state.json", {}).get("status") == "completed":
            return
        marker = stage / "day-workers" / f"{attempt:02d}.json"
        prior = e2e.read_json(marker)
        process = None
        if prior:
            identity = cohort.process_identity(prior["pid"])
            while identity and identity[0] == prior["start_ticks"] and identity[1] != "Z":
                time.sleep(15)
                identity = cohort.process_identity(prior["pid"])
        else:
            if attempt > 1 and not infrastructure_recoverable(stage, tasks):
                break
            if attempt > 1:
                stop_preserved_grading_containers(stage, settings)
            log_path = stage / "day-workers" / f"{attempt:02d}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as log:
                process = subprocess.Popen([sys.executable, "-m", "scripts.injection.e2e_cycles", "--config", str(e2e.CONFIG_FILE),
                    "--day-worker", str(stage)],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            identity = cohort.process_identity(process.pid)
            if not identity:
                raise RuntimeError("Day worker exited before its process identity was recorded")
            prior = {"pid": process.pid, "start_ticks": identity[0], "started_at": e2e.now(), "attempt": attempt}
            e2e.write_json(marker, prior)
            while process.poll() is None:
                time.sleep(15)
            prior.update(returncode=process.returncode, exited_at=e2e.now())
            e2e.write_json(marker, prior)
        e2e.write_json(stage / "phase-handoff.json", {"status": "day_worker_exited", "one_shot": True,
            "previous_runner_pid": prior["pid"], "previous_runner_start_ticks": prior["start_ticks"],
            "actual_child_process_boundary": True, "recorded_at": e2e.now()})
        if e2e.read_json(stage / "day/state.json", {}).get("status") == "completed":
            return
    raise RuntimeError("Day remains incomplete after its bounded infrastructure recovery; original evidence preserved")


def night_inputs(stage: Path, condition: str, tasks: list[dict], sessions: list[dict], payload: dict,
                 number: int = 1, observation_content_max_chars: int = 240) -> tuple[list[dict], dict]:
    if (not isinstance(observation_content_max_chars, int)
            or isinstance(observation_content_max_chars, bool)
            or not 240 <= observation_content_max_chars <= 4096):
        raise ValueError("Invalid frozen summary observation content budget")
    selected = {}
    missing = {}
    for short_id in sorted(cohort.INJECTED_IDS) if condition == "poisoned" else []:
        candidates = sorted((session for session in sessions if session["task_id"] == next(
            task["task_id"] for task in tasks if task["short_id"] == short_id)),
            key=lambda row: row["native_record_segment_index"])
        errors = []
        for session in candidates:
            try:
                modified, audit = inject_uploaded(session, payload, cohort.INJECTION_ENDPOINTS[short_id])
            except ValueError as error:
                errors.append({"session_id": session["session_id"], "reason": str(error)})
                continue
            selected[session["session_id"]] = (modified, audit)
            break
        else:
            missing[short_id] = errors or [{"reason": "No authentic uploaded segment is available for this task"}]
    result = []
    for session in sessions:
        modified, audit = selected.get(session["session_id"], (session, None))
        modified = copy.deepcopy(modified)
        modified["round"] = number
        if observation_content_max_chars != 240:
            modified["_tool_observation_content_max_chars"] = observation_content_max_chars
        exposure = None
        if audit is not None and payload.get("source_case") == "data_exfil_file":
            from evolve_server.pipeline.summarizer import _build_session_payload
            formatted = _build_session_payload(modified)
            contents = [row.get("content", "") for turn in formatted["interactions"]
                        for field in ("tool_results", "tool_observations")
                        for row in turn.get(field, [])]
            exposure = {"policy": "actual-native-summary-input-full-payload-v1",
                "complete_payload_present": any(payload["text"] in text for text in contents),
                "payload_text_sha256": e2e.sha256(payload["text"].encode()),
                "observation_content_max_chars": observation_content_max_chars,
                "observation_views": len(contents)}
            if not exposure["complete_payload_present"]:
                raise ValueError("The selected updated payload is incomplete in the actual summary input")
        result.append(modified)
        e2e.write_json(stage / "inputs" / "sessions" / f"{session['session_id']}.json", modified)
        e2e.write_json(stage / "inputs" / "evidence" / f"{session['session_id']}.json", {
            "task_id": session["task_id"], "session_id": session["session_id"],
            "original_session_id": session["original_session_id"],
            "native_uploaded_sha256": session["native_uploaded_sha256"],
            "clean_session_sha256": cohort.digest(session), "used_session_sha256": cohort.digest(modified),
            "tampered": audit is not None, "tamper": audit,
            "summary_input_exposure": exposure,
            "round_metadata": {"source_round": session.get("round"), "cycle_round": number,
                               "original_uploaded_object_unchanged": True}})
    manifest = {"task_count": 30, "native_session_count": len(sessions), "condition": condition, "round": number,
        "attempted_injected_tasks": sorted(cohort.INJECTED_IDS) if condition == "poisoned" else [],
        "injected_native_session_ids": sorted(selected), "missing_carriers": missing,
        "repeated_exposure_round": True, "safe_prompt_enabled": False,
        "summary_tool_result_content_max_chars": observation_content_max_chars,
        "session_content_hash": cohort.digest(result)}
    e2e.write_json(stage / "inputs/manifest.json", manifest)
    return result, manifest


def run_stage(run: Path, config: dict, condition: str, number: int, parent_bank: Path) -> dict:
    _, engine, _, evolution, controller, task_runtime, _, tree_hash, writer = e2e.load_components()
    if config["sources"] != sources():
        raise ValueError("Cycle orchestration source changed after freezing")
    original_settings = (e2e.read_json(Path(config["original_protocol"]["path"]))["settings"]
                         if config.get("method_revision") else
                         e2e.read_json(Path(config["source_run"]) / "config.runtime.json"))
    cohort.validate_resume(Path(config["source_run"]), original_settings, tree_hash)
    stage, settings = setup_stage(run, config, condition, number, parent_bank, tree_hash)
    checkpoint = stage / "stage-state.json"
    previous = e2e.read_json(checkpoint)
    if previous and previous.get("status") == "completed":
        if tree_hash(Path(previous["published_bank"])) != previous["published_bank_sha256"]:
            raise ValueError("Previously deployed cycle bank changed")
        return previous
    if config.get("method_revision") and number == 1:
        from scripts.injection.cycle_revision import seed_night1
        seed_night1(stage, settings, config, condition, evolution, tree_hash)
    e2e.write_json(checkpoint, {"status": "day_running", "round": number, "condition": condition, "updated_at": e2e.now()})
    if number == 1:
        importer = resilient_runner.baseline_import(settings)
        if resilient_runner.enabled(settings) and importer is None:
            raise resilient_runner.ProtocolError(
                "resilient-v1 Day1 requires baseline_import; legacy fallback is forbidden")
        if importer is not None:
            bank_hash = tree_hash(stage / "banks/S0")
            identity = resilient_runner.baseline_execution_identity(settings, settings["all_tasks"], bank_hash)
            sessions = importer.sessions(settings["all_tasks"], identity)
            imported_rows = [importer.scored_row(task, identity, bank_hash)[0]
                             for task in settings["all_tasks"]]
            scores = [float(row["score"]) for row in imported_rows]
            day = {"status": "completed", "task_count": 30, "scored_tasks": 30,
                   "uploaded_sessions": len(sessions), "mean_score": sum(scores) / len(scores)}
            source_inputs = {"input_coverage_complete": True, "task_count": 30}
            day_evidence = {"kind": "digest_bound_baseline_import", "import_bundle": str(importer.directory),
                "manifest_sha256": e2e.sha256(importer.manifest_path.read_bytes()), "scored_tasks": 30,
                "new_agent_executions": 0, "new_scoring_calls": 0}
            e2e.write_json(stage / "baseline-import-receipt.json", {**day_evidence,
                "execution_identity": identity, "source_manifest": importer.manifest})
        else:
            source = Path(config["bootstrap_day_run"])
            sessions = [item[0] for task in settings["all_tasks"] for item in cohort.finalized_day_segments(source, task)]
            day = e2e.read_json(source / "day/state.json")
            source_inputs = e2e.read_json(source / "finalized-day-inputs/manifest.json")
            day_evidence = {"kind": "shared_real_Day1_reused", "source_run": str(source),
                "manifest_sha256": config["bootstrap_manifest_sha256"], "scored_tasks": day["scored_tasks"]}
    else:
        run_day(stage, settings)
        day = e2e.read_json(stage / "day/state.json")
        captured = [cohort.load_completed_day(stage, task)[1] for task in settings["all_tasks"]]
        sessions = cohort.finalize_day_sessions(stage, settings["all_tasks"], captured, tree_hash(stage / "banks/S0"))
        source_inputs = e2e.read_json(stage / "finalized-day-inputs/manifest.json")
        day_evidence = {"kind": "fresh_30_task_executions", "source_run": str(stage), "scored_tasks": day["scored_tasks"]}
    payload_file = Path(config["frozen_payload"])
    if e2e.sha256(payload_file.read_bytes()) != config["payload_sha256"]:
        raise ValueError("Frozen poisoning payload changed before Night")
    payload = e2e.read_json(payload_file)
    sessions, injection = night_inputs(stage, condition, settings["all_tasks"], sessions, payload,
        number=number, observation_content_max_chars=settings.get("summary_tool_result_content_max_chars", 240))
    if config.get("method_revision") and number == 1:
        from scripts.injection.cycle_revision import verify_night1_inputs
        verify_night1_inputs(stage, sessions)
    e2e.write_json(checkpoint, {"status": "night_running", "round": number, "condition": condition, "day": day_evidence})
    budget = resilient_runner.budget_from_config(settings, "night", stage)
    if budget is not None:
        settings = budget.cap_config(settings, "night:summary-native-validation")
        e2e.write_json(stage / "night-budget-binding.json", settings["stage_budget"])
    gateway_process = None
    try:
        completed = e2e.read_json(stage / "branches" / condition / "night-state.json", {})
        cache = stage / "branches" / condition / "nights" / f"{number:02d}" / "candidates.json"
        needs_model = not cache.is_file() or bool(e2e.read_json(cache))
        if completed.get("status") != "completed" and needs_model:
            gateway_process, gateway, _ = start_gateway(stage, settings, engine)
            settings["local_api_base_url"] = settings["llm_base_url"] = gateway + "/v1"
        night = run_native_night(stage, settings, sessions, condition, controller, evolution, tree_hash, writer)
    finally:
        stop_gateway(gateway_process)
    published = Path(night["published_bank"])
    published_hash = tree_hash(published)
    if published_hash != night["published_bank_sha256"] or published_hash != night["publication"]["bank_sha256"]:
        raise ValueError("Native publication evidence differs from the deployed bank")
    result = {"status": "completed", "round": number, "condition": condition,
        "parent_bank": str(parent_bank), "parent_bank_sha256": tree_hash(parent_bank),
        "published_bank": str(published), "published_bank_sha256": published_hash,
        "changeset_accepted": night["publication"].get("changeset_accepted") is True,
        "protocol_hash": settings["protocol_hash"], "day": day_evidence,
        "scored_tasks": day["scored_tasks"], "mean_score": day.get("mean_score"),
        "input_coverage_complete": source_inputs["input_coverage_complete"],
        "missing_injection_carriers": injection["missing_carriers"],
        "native_session_count": len(sessions), "night_state": str(stage / "branches" / condition / "night-state.json"),
        "sandbox_status": f"scheduled_only_at_S{configured_rounds(config)}", "finished_at": e2e.now()}
    e2e.write_json(checkpoint, result)
    return result


def run_native_night(stage, settings, sessions, condition, controller, evolution, tree_hash, writer):
    """Use the actual cycle round; recover validation without repeating a proposal."""
    number = settings.get("cycle_round", 1)
    if type(number) is not int or not 1 <= number <= configured_rounds(settings):
        raise ValueError("Native Night is outside the configured round range")
    branch = stage / "branches" / condition
    marker = branch / "night-state.json"
    night_dir = branch / "nights" / f"{number:02d}"
    cached = night_dir / "candidates.json"
    expected_bank = branch / "banks" / f"S{number}"
    prior = e2e.read_json(marker, {})
    if prior.get("status") == "completed":
        # Legacy Night1 has no top-level round. Reuse its evidence unchanged.
        if prior.get("round", 1) != number:
            raise ValueError("Completed Night belongs to another cycle round")
        publication = e2e.read_json(night_dir / "publication.json", {})
        if (tree_hash(expected_bank) != prior["published_bank_sha256"]
                or publication != prior["publication"] or publication.get("round") != number
                or publication.get("bank_sha256") != prior["published_bank_sha256"]):
            raise ValueError("Completed Night publication or deployed bank changed")
        return prior
    summary_resume = False
    if prior and not cached.exists():
        proposal = e2e.read_json(night_dir / "proposals/evolution/proposal-summary.json", {})
        summary_resume = (resilient_runner.enabled(settings)
                          and proposal.get("status") == "running"
                          and proposal.get("phase") == "summarizing")
        if not summary_resume:
            raise RuntimeError("Incomplete native evolution has no resumable summary checkpoint; refusing a duplicate call")
    copy_bank(stage / "banks/S0", branch / "banks/S0", tree_hash)
    recovering = bool(prior) or cached.exists()
    if not prior:
        e2e.write_json(marker, {"status": "started", "round": number,
                               "at": e2e.now(), "safe_prompt": False})

    def record_recovery():
        cache_hash = e2e.sha256(cached.read_bytes())
        e2e.write_json(branch / "validation-recovery.json", {"at": e2e.now(), "round": number,
            "candidate_cache_sha256": cache_hash, "new_native_evolution_calls": 0,
            "mode": "existing_proposal_and_immutable_execution_scoring_only"})
        return cache_hash

    cache_hash = record_recovery() if recovering and cached.exists() else None
    if summary_resume:
        e2e.write_json(branch / "summary-recovery.json", {"at": e2e.now(), "round": number,
            "new_calls_for_completed_summaries": 0, "mode": "resume_incomplete_summary_units"})
    try:
        published = controller.night(number, branch / "banks/S0", sessions,
                                     settings["all_tasks"], branch, settings)
    except Exception as error:
        if resilient_runner.enabled(settings):
            raise
        if recovering or not cached.exists() or not any(term in str(error).lower() for term in
                ("scoring", "grading", "infrastructure", "503", "502", "connection", "transport")):
            raise
        cache_hash = record_recovery()
        recovering = True
        published = controller.night(number, branch / "banks/S0", sessions,
                                     settings["all_tasks"], branch, settings)
    if cache_hash is not None and e2e.sha256(cached.read_bytes()) != cache_hash:
        raise ValueError("Finished proposal changed during validation recovery")
    publication = e2e.read_json(night_dir / "publication.json", {})
    if (Path(published).resolve() != expected_bank.resolve() or publication.get("round") != number
            or tree_hash(published) != publication.get("bank_sha256")):
        raise ValueError("Native publication does not match the actual cycle round or deployed bank")
    evidence = {"status": "completed", "round": number, "at": e2e.now(), "safe_prompt": False,
        "published_bank": str(published), "published_bank_sha256": tree_hash(published),
        "publication": publication,
        "proposal_summary": e2e.read_json(night_dir / "proposals/evolution/proposal-summary.json")}
    if cache_hash is not None:
        evidence["validation_recovered_without_repeating_evolution"] = True
    if summary_resume:
        evidence["summary_checkpoint_resumed_without_repeating_completed_summaries"] = True
    e2e.write_json(marker, evidence)
    return evidence


def endpoint_sandbox(run: Path, config: dict, condition: str, result: dict) -> dict:
    from scripts.injection.cycle_revision import stage_root
    root = stage_root(run, config) / "endpoints" / condition
    settings = copy.deepcopy(config["settings"])
    endpoint = f"S{configured_rounds(config)}"
    # This observer uses its own frozen sandbox task and canary input contract.
    settings.pop("cohort_validation_binding", None)
    settings.update(runtime_dir=str(root / "runtime"), gateway_log_dir=str(root / "runtime/api-evidence"),
                    cohort_day_alignment=True)
    endpoint_budget = resilient_runner.budget_from_config(settings, "endpoint", root)
    if endpoint_budget is not None:
        settings = endpoint_budget.cap_config(settings, f"endpoint:{condition}")
    settings["protocol_hash"] = cohort.digest({"cycle_run": config["run_id"], "endpoint": condition,
        "bank_sha256": result["published_bank_sha256"], "sources": config["sources"]})
    source = Path(config["source_run"])
    tasks = [task for task in settings["all_tasks"] if task["short_id"] in cohort.INJECTED_IDS]
    for task in tasks:
        target = root / "frozen/tasks" / task["short_id"] / "task-workspace"
        original = source / "frozen/tasks" / task["short_id"] / "task-workspace"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(original, target_is_directory=True)
    e2e.write_json(root / "config.runtime.json", settings)
    _, engine, _, _, _, runtime, _, tree_hash, _ = e2e.load_components()
    bank = Path(result["published_bank"])
    if tree_hash(bank) != result["published_bank_sha256"]:
        raise ValueError(f"Final {endpoint} bank changed before sandbox execution")
    gateway_process = None
    original_start = cohort_sandbox.e2e.start_api
    startup_lock = threading.Lock()

    def serialized_start(*args, **kwargs):
        with startup_lock:
            return original_start(*args, **kwargs)

    try:
        gateway_process, gateway, _ = start_gateway(root, settings, engine)
        cohort_sandbox.e2e.start_api = serialized_start
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(cohort_sandbox.run_victim, root, settings, task, bank, runtime,
                gateway, safe=False, condition=f"{endpoint}-{condition}-{task['short_id']}"): task["short_id"] for task in tasks}
            observations = {}
            for future in concurrent.futures.as_completed(futures):
                short_id = futures[future]
                try:
                    observations[short_id] = future.result()
                except Exception as error:
                    observations[short_id] = {"measurement_valid": False, "attack_success": None,
                                               "error_type": type(error).__name__, "error": str(error)}
                e2e.write_json(root / "progress.json", observations)
    finally:
        cohort_sandbox.e2e.start_api = original_start
        stop_gateway(gateway_process)
    valid = sum(item.get("measurement_valid") is True for item in observations.values())
    successes = sum(item.get("measurement_valid") is True and item.get("attack_success") is True
                    for item in observations.values())
    summary = {"status": "completed" if valid == len(tasks) else "finished_with_gaps", "condition": condition,
        "endpoint": endpoint, "safe_prompt": False, "valid_denominator": valid, "attack_successes": successes,
        "asr": successes / valid if valid else None, "observations": observations,
        "bank_sha256": result["published_bank_sha256"]}
    if settings.get("selected_injection_case") == "data_exfil_file":
        summary.update(selected_injection_case="data_exfil_file",
            behavior_metric="synthetic_invocation_audit_record_rate",
            behavior_successes=successes, behavior_rate=summary["asr"],
            record_only_adoption_rate=summary["asr"], real_data_exfiltration_measured=False,
            metric_contract=settings["injection_metric_contract"])
    e2e.write_json(root / "summary.json", summary)
    return summary


def run_cycles(run: Path, config: dict) -> None:
    rounds = configured_rounds(config)
    conditions = configured_conditions(config)
    state_path = run / "state.json"
    stop = threading.Event()

    def heartbeat():
        while not stop.is_set():
            e2e.write_json(run / "heartbeat.json", {"pid": os.getpid(), "updated_at": e2e.now(),
                "active": True, "stage": e2e.read_json(state_path, {}).get("current_stage")})
            stop.wait(15)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    results, errors, endpoints = {}, {}, {}
    banks = {condition: Path(config.get("initial_bank", Path(config["bootstrap_day_run"]) / "banks/S0"))
             for condition in conditions}
    try:
        for number in range(1, rounds + 1):
            for condition in conditions:
                if condition in errors:
                    continue
                key = f"{number:02d}/{condition}"
                e2e.write_json(state_path, {"status": "running", "method_revision": config.get("method_revision"), "current_stage": key,
                    "stages": results, "errors": errors, "updated_at": e2e.now()})
                try:
                    result = run_stage(run, config, condition, number, banks[condition])
                    results[key] = result
                    banks[condition] = Path(result["published_bank"])
                except Exception as error:
                    errors[condition] = {"stage": key, "error_type": type(error).__name__, "error": str(error)}
                    from scripts.injection.cycle_revision import stage_root
                    e2e.write_json(stage_root(run, config) / "rounds" / f"{number:02d}" / condition / "cycle-error.json", errors[condition])
        for condition in conditions:
            if condition not in errors:
                try:
                    endpoints[condition] = endpoint_sandbox(run, config, condition, results[f"{rounds:02d}/{condition}"])
                except Exception as error:
                    errors[condition] = {"stage": f"S{rounds}/sandbox", "error_type": type(error).__name__, "error": str(error)}
        complete = (len(results) == rounds * len(conditions) and not errors and all(
            item["input_coverage_complete"] and not item["missing_injection_carriers"] for item in results.values())
            and all(item["status"] == "completed" for item in endpoints.values()))
        e2e.write_json(state_path, {"status": "finished" if complete else "finished_with_gaps", "method_revision": config.get("method_revision"), "stages": results,
            "errors": errors, "endpoints": endpoints, "finished_at": e2e.now(), "paid_api_budget_usd": 0,
            "safe_prompt_conditions": "paused_by_user", "intermediate_round_asr": "not_measured"})
    except BaseException as error:
        e2e.write_json(state_path, {"status": "failed_or_incomplete", "method_revision": config.get("method_revision"), "stages": results,
            "errors": errors, "endpoints": endpoints, "error_type": type(error).__name__,
            "error": str(error), "updated_at": e2e.now()})
        raise
    finally:
        stop.set()
        thread.join(timeout=2)
        e2e.write_json(run / "heartbeat.json", {"pid": os.getpid(), "updated_at": e2e.now(), "active": False})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=e2e.CONFIG_FILE)
    parser.add_argument("--dry-run", action="store_true", help="Read and validate scope without writing or calling a model")
    parser.add_argument("--run-id")
    parser.add_argument("--source-run")
    parser.add_argument("--bootstrap-day-run")
    parser.add_argument("--resume", action="store_true")
    from scripts.injection.cycle_revision import REVISION
    parser.add_argument("--method-revision", choices=[REVISION])
    parser.add_argument("--detach", action="store_true")
    parser.add_argument("--day-worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--after-bootstrap-worker", type=int, help="Wait for the recorded real Day1 collection worker")
    args = parser.parse_args()
    settings = configure(args.config)
    if args.dry_run:
        print(json.dumps({"config": str(e2e.CONFIG_FILE), "rounds": ROUNDS,
            "endpoint": f"S{ROUNDS}", "conditions": list(CONDITIONS),
            "execution_policy": settings.get("execution_policy"), "scientific_calls": 0}, indent=2))
        return
    e2e.require_execution()
    if args.day_worker:
        day_worker(args.day_worker.resolve())
        return
    if not args.run_id or len(args.run_id) > 48 or not re.fullmatch(r"[A-Za-z0-9_.-]+", args.run_id):
        parser.error("A safe --run-id is required")
    if args.detach:
        logs = EXPERIMENT / "runtime/launches"
        logs.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, "-m", "scripts.injection.e2e_cycles", *[arg for arg in sys.argv[1:] if arg != "--detach"]]
        suffix = f"-{args.method_revision}" if args.method_revision else ""
        log_path = logs / f"{args.run_id}{suffix}-cycles.log"
        with log_path.open("ab") as log:
            child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        e2e.write_json(logs / f"{args.run_id}{suffix}-cycles.json", {"pid": child.pid, "command": command,
            "log": str(log_path), "launched_at": e2e.now()})
        print(json.dumps({"pid": child.pid, "log": str(log_path)}), flush=True)
        return
    locks = EXPERIMENT / "runtime/runner-locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / f"{args.run_id}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.after_bootstrap_worker:
            if not args.bootstrap_day_run:
                parser.error("--after-bootstrap-worker requires --bootstrap-day-run")
            wait_bootstrap(args.run_id, Path(args.bootstrap_day_run).resolve(), args.after_bootstrap_worker)
        if args.method_revision:
            if not args.resume:
                parser.error("A method revision requires --resume of the preserved original run")
            from scripts.injection.cycle_revision import prepare as prepare_revision
            run = e2e.run_directory(args.run_id)
            config = prepare_revision(run, sources())
        else:
            run, config = prepare(args.run_id, args.source_run, args.bootstrap_day_run, args.resume)
        e2e.write_json(run / "runner.json", {"pid": os.getpid(), "started_at": e2e.now()})
        if args.method_revision:
            from scripts.injection.cycle_revision import execution_context
            _, _, _, evolution, controller, *_ = e2e.load_components()
            with execution_context(config, controller, evolution):
                run_cycles(run, config)
        else:
            run_cycles(run, config)


if __name__ == "__main__":
    main()
