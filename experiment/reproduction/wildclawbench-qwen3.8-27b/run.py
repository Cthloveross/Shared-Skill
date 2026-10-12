#!/usr/bin/env python3
"""Resumeable six-day/six-night experiment with real task A/B and endpoint controls."""

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
from protocol import (
    ROOT,
    accept_candidate,
    canonical_hash,
    create_initial_bank,
    discover_tasks,
    load_config,
    public_config,
    tree_hash,
    write_json,
)
from reports import generate

sys.path.insert(0, str(ROOT.parents[2]))

LOG_LOCK = threading.Lock()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def event(run_dir: Path, kind: str, **fields) -> None:
    record = {"timestamp": now(), "event": kind, **fields}
    with LOG_LOCK:
        with (run_dir / "events.jsonl").open("a") as stream:
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
    source_paths = [p for p in ROOT.glob("*.py") if not p.name.startswith("test_") and p.name != "reports.py"]
    source_paths += list((ROOT / "services").glob("*.py"))
    for package in ("sharedskill", "evolve_server"):
        source_paths += list((ROOT.parents[2] / package).rglob("*.py"))
    # The native evolver reads this prompt at night; freezing only Python would
    # allow its instructions to change without invalidating a resumed trial.
    source_paths.append(ROOT.parents[2] / "evolve_server/engines/EVOLVE_AGENTS.md")
    for path in sorted(source_paths):
        sources[path.relative_to(ROOT.parents[2]).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    document = {"config": public_config(config), "tasks": tasks, "source_hashes": sources}
    destination = run_dir / "frozen-protocol.json"
    if destination.exists():
        if json.loads(destination.read_text()) != document:
            raise ValueError("Configuration or task bytes changed; use a new run instead of reusing results")
    else:
        write_json(destination, document)
    inventory = run_dir / "startup-inventory.json"
    if not inventory.exists():

        def command(args):
            process = subprocess.run(args, capture_output=True, text=True, check=False)
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
                "fixed_execution_count": 480,
                "pilot_execution_count": 8,
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


def run_one(
    task: dict, bank: Path, run_dir: Path, config: dict, phase: str, number: int, arm: str = "", candidate_id: str = ""
) -> dict:
    from task_runtime import execute_task, rescore_task

    if task.get("input_sha256"):
        from provenance import validate_task_inputs

        validate_task_inputs(config, task)
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
    execution_hash = canonical_hash(identity)
    directory = (
        run_dir
        / "executions"
        / phase
        / f"{number:02d}"
        / (candidate_id or "main")
        / (arm or "shared")
        / task["short_id"]
    )
    directory.mkdir(parents=True, exist_ok=True)
    result_path = directory / "result.json"
    previous = json.loads(result_path.read_text()) if result_path.exists() else None
    if previous and previous.get("execution_hash") != execution_hash:
        raise ValueError(f"Refusing cross-bank/config result reuse: {directory}")
    if previous and previous.get("status") == "completed":
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
    for attempt in range(config["infrastructure_retries"] + 1):
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
        if attempt < config["infrastructure_retries"]:
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
    generate(run_dir, config["all_tasks"])
    pending = [r for r in results if r["status"] != "completed" or r["score"] is None]
    if pending:
        raise RuntimeError(
            f"{phase} {number}: {len(pending)} tasks need infrastructure/score repair; "
            "resume reuses completed executions"
        )
    return sorted(results, key=lambda r: r["short_id"])


def outcome_preference(old: list[dict], new: list[dict], directory: Path, config: dict) -> str:
    """Blind per-task Qwen comparison, using actual outputs and grading evidence."""
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
            "model": config["model_id"],
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
        response = httpx.post(
            config["local_api_base_url"].removesuffix("/v1") + f"/runs/{comparison_id}/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer " + config["local_api_key"]},
            timeout=1800,
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


def night(number: int, bank: Path, sessions: list[dict], tasks: list[dict], run_dir: Path, config: dict) -> Path:
    from evolution import propose_candidates

    directory = run_dir / "nights" / f"{number:02d}"
    directory.mkdir(parents=True, exist_ok=True)
    published = run_dir / "banks" / f"S{number}"
    publication_file = directory / "publication.json"
    if publication_file.exists():
        publication = json.loads(publication_file.read_text())
        if tree_hash(published) != publication["bank_sha256"] or tree_hash(bank) != publication["parent_sha256"]:
            raise RuntimeError("Published bank or parent hash changed")
        return published
    candidates_file = directory / "candidates.json"
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
    from interface_checks import verify_interfaces

    verify_interfaces(config, run_dir / "interfaces")
    update_state(run_dir, "pilot")
    selected = [t for t in tasks if t["short_id"] in config["pilot_short_ids"]]
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
            "fixed_execution_count": 480,
            "extrapolated_fixed_wall_hours": elapsed * 480 / len(result) / 3600,
            "limitations": "Eight selected difficult tasks; excludes all night proposal/A-B overhead. "
            "GPU contention prevents linear scaling.",
        },
    )
    event(
        run_dir,
        "pilot_finished",
        tasks=len(result),
        seconds=elapsed,
        extrapolated_fixed_hours=elapsed * 480 / len(result) / 3600,
    )
    update_state(run_dir, "pilot_complete")
    generate(run_dir, tasks)


def run_experiment(tasks: list[dict], initial: Path, run_dir: Path, config: dict):
    check_services(config)
    gate = run_dir / "pilot-gate.json"
    if not gate.exists() or not json.loads(gate.read_text()).get("passed"):
        pilot(tasks, initial, run_dir, config)
    bank = initial
    for number in range(1, 7):
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
            arms = [("S0", initial), ("S6", bank)]
            random.Random(config["seed"] + int(task["short_id"].replace(".", ""))).shuffle(arms)
            for arm, selected_bank in arms:
                result = run_one(task, selected_bank, run_dir, config, "endpoint", 6, arm)
                if result["status"] != "completed" or result["score"] is None:
                    raise RuntimeError("Endpoint pair needs infrastructure/score repair")

    assignments = [[t for t in tasks if t["user_id"] == f"user-{i + 1:02d}"] for i in range(config["users"])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=config["users"]) as pool:
        for future in concurrent.futures.as_completed([pool.submit(endpoint_worker, group) for group in assignments]):
            future.result()
    update_state(run_dir, "complete", completed_at=now())
    generate(run_dir, tasks)
    event(run_dir, "experiment_complete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "pilot", "run", "resume", "report", "status"])
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--run-dir", type=Path)
    arguments = parser.parse_args()
    config = load_config(arguments.config)
    if arguments.command == "prepare":
        from environment import prepare_sources

        prepare_sources(config)
    tasks = discover_tasks(config)
    if arguments.command == "prepare":
        from environment import prepare

        initial = ROOT / "runtime/prepared/S0"
        bank_hash = create_initial_bank(config, initial)
        write_json(ROOT / "runtime/prepared/task-manifest.json", tasks)
        preparation = prepare(config)
        if preparation.get("status") == "ready":
            from provenance import prepare_input_manifest

            tasks = prepare_input_manifest(config, tasks)
            write_json(ROOT / "runtime/prepared/task-manifest.json", tasks)
        preparation["initial_bank_sha256"] = bank_hash
        write_json(ROOT / "runtime/prepared/readiness.json", preparation)
        print(json.dumps(preparation, ensure_ascii=False, indent=2))
        return
    latest = ROOT / "runtime/latest-run.txt"
    run_dir = arguments.run_dir
    if run_dir is None:
        if latest.exists():
            run_dir = Path(latest.read_text().strip())
        elif arguments.command in {"pilot", "run"}:
            run_dir = ROOT / "runtime/runs" / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        else:
            raise RuntimeError("No run exists; prepare, then pilot/run")
    run_dir = run_dir.resolve()
    if arguments.command == "status":
        status = (
            json.loads((run_dir / "state.json").read_text())
            if (run_dir / "state.json").exists()
            else {"status": "not_started"}
        )
        counts = {}
        for path in (run_dir / "executions").rglob("result.json"):
            result = json.loads(path.read_text())
            if not result.get("experiment_record"):
                continue
            phase = result["phase"]
            summary = counts.setdefault(phase, {"completed": 0, "pending_score": 0, "infrastructure_error": 0})
            key = result.get("status", "infrastructure_error")
            summary[key] = summary.get(key, 0) + 1
        status.update(run_dir=str(run_dir), executions=counts, fixed_execution_count=480)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    from provenance import prepare_input_manifest

    tasks = prepare_input_manifest(config, tasks)
    with exclusive_run(run_dir):
        config["protocol_hash"] = freeze(config, tasks, run_dir)
        config["all_tasks"] = tasks
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(str(run_dir) + "\n")
        if arguments.command == "report":
            print(json.dumps(generate(run_dir, tasks), ensure_ascii=False, indent=2))
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
