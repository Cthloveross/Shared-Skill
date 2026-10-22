#!/usr/bin/env python3
"""Small, resumable diagnostics against explicitly selected historical evidence."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import signal
import time
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.wildclawbench import environment
import httpx
from scripts.wildclawbench.protocol import REPOSITORY, output_root, canonical_hash, discover_tasks, load_config, tree_hash, write_json
from scripts.wildclawbench.run import exclusive_run, freeze, run_one

SHORT_IDS = ("01.02", "02.11", "03.01", "04.04", "05.11", "06.07")
OLD_ROOT: Path | None = None  # Explicit historical_run_dir is required by this diagnostic CLI.


def historical_input(tasks: list[dict]) -> dict:
    frozen = json.loads((OLD_ROOT / "frozen-protocol.json").read_text())
    expected = {task["task_id"]: task for task in tasks}
    previous = {task["task_id"]: task for task in frozen["tasks"]}
    if set(previous) != set(expected):
        raise ValueError("Historical and diagnostic task sets differ")
    evidence = []
    old_protocol = canonical_hash(frozen)
    old_bank = tree_hash(OLD_ROOT / "banks/S0")
    for task_id, task in sorted(expected.items()):
        for field in ("source_sha256", "timeout_seconds", "input_sha256", "user_id"):
            if previous[task_id][field] != task[field]:
                raise ValueError("Historical task condition differs: " + task_id + ":" + field)
        path = OLD_ROOT / "executions/day/01/main/shared" / task["short_id"] / "result.json"
        result = json.loads(path.read_text())
        identity = {"phase": "day", "round": 1, "arm": "", "candidate_id": "", "task_id": task_id,
                    "bank": old_bank, "config": old_protocol, "input": task["input_sha256"],
                    "seed": frozen["config"]["seed"]}
        if (result["task_id"] != task_id or result["status"] != "completed" or result.get("score") is None
                or result["phase"] != "day" or result["round"] != 1
                or result["user_id"] != task["user_id"] or result["input_sha256"] != task["input_sha256"]
                or result["skill_bank_sha256"] != old_bank
                or result["execution_hash"] != canonical_hash(identity)):
            raise ValueError("Invalid historical Day1 execution: " + task_id)
        session = result["session"]
        for field in ("task_id", "user_id", "phase", "round", "skill_bank_sha256"):
            if session.get(field) != result[field]:
                raise ValueError("Historical session identity differs: " + task_id + ":" + field)
        if session.get("session_id") != result["execution_hash"][:24]:
            raise ValueError("Historical session ID differs: " + task_id)
        evidence.append({"task_id": task_id, "path": str(path),
                         "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "session_sha256": canonical_hash(result["session"]),
                         "session_id": result["session"]["session_id"]})
    return {"source_run": str(OLD_ROOT), "sessions": evidence,
            "banks": {arm: tree_hash(OLD_ROOT / "banks" / arm) for arm in ("S0", "S6")},
            "human_video_contract_injected_into_new_evolution": False}


def prepare(config: dict, run_dir: Path) -> tuple[dict, list[dict]]:
    runtime = Path(config["runtime_dir"])
    shared = Path(config["shared_runtime_dir"])
    (runtime / "prepared").mkdir(parents=True, exist_ok=True)
    upstream = runtime / "upstream"
    if not upstream.exists():
        upstream.symlink_to(shared / "upstream", target_is_directory=True)
    origin = shared / "prepared/input-files.json"
    target = runtime / "prepared/input-files.json"
    if target.exists() and target.read_bytes() != origin.read_bytes():
        raise ValueError("Input provenance baseline changed")
    if not target.exists():
        shutil.copy2(origin, target)
    manifest = json.loads(target.read_text())
    tasks = []
    for task in discover_tasks(config):
        record = manifest["tasks"][task["task_id"]]
        tasks.append({**task, "input_sha256": record["input_sha256"],
                      "input_file_count": record["file_count"], "input_bytes": record["bytes"]})
    run_dir.mkdir(parents=True, exist_ok=True)
    config["historical_diagnostic_input"] = historical_input(tasks)
    # Freeze before adding runtime protocol_hash/all_tasks to avoid self-reference.
    config["protocol_hash"] = freeze(config, tasks, run_dir)
    config["all_tasks"] = tasks
    banks = {}
    for arm in ("S0", "S6"):
        source, destination = OLD_ROOT / "banks" / arm, run_dir / "banks" / arm
        if not destination.exists():
            shutil.copytree(source, destination)
        if tree_hash(source) != tree_hash(destination):
            raise ValueError("Historical bank copy differs: " + arm)
        banks[arm] = {"source": str(source), "sha256": tree_hash(destination),
                      "historical_manual_edits": arm == "S6"}
    write_json(run_dir / "diagnostic-design.json", {
        "protocol_hash": config["protocol_hash"], "short_ids": list(SHORT_IDS), "executions": 12,
        "claim": "execution-chain diagnostic, not evolved-skill benefit",
        "banks": banks, "native_evolution_input": "all 30 real historical Day1 sessions and complete S0",
        "paid_budget_usd": 1, "six_day_rerun": False,
    })
    # Gateway needs exactly the same explicit protocol hash as task bindings.
    gateway_config = {key: value for key, value in config.items() if key != "all_tasks"}
    write_json(run_dir / "gateway-config.json", gateway_config)
    return config, tasks


def stop_gateway(ownership: dict | None) -> None:
    """Stop only a router this diagnostic started, preserving shared services."""
    if ownership is None:
        return
    from scripts.wildclawbench.services.router_control import owned

    if not owned(ownership):
        return
    os.killpg(ownership["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 5
    while owned(ownership) and time.monotonic() < deadline:
        time.sleep(0.1)
    if owned(ownership):
        os.killpg(ownership["pid"], signal.SIGKILL)


def start_gateway(config: dict, run_dir: Path) -> dict | None:
    health = config["local_api_base_url"].removesuffix("/v1") + "/healthz"
    try:
        response = httpx.get(health, timeout=3, trust_env=False)
    except httpx.HTTPError:
        response = None
    evidence = Path(config.get("gateway_log_dir", Path(config["runtime_dir"]) / "api-evidence")) / "router-config.json"
    if response is not None:
        if not evidence.exists() or json.loads(evidence.read_text()).get("protocol_hash") != config["protocol_hash"]:
            raise RuntimeError("Port occupied by a different protocol; will not replace its service")
        response.raise_for_status()
        return None
    logs = output_root(run_dir, config) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    from urllib.parse import urlsplit
    port = config.get("router_port", urlsplit(config["local_api_base_url"]).port or 80)
    with (logs / "gateway.log").open("ab") as stream:
        process = subprocess.Popen([sys.executable, "-m", "scripts.wildclawbench.gateway",
                                    "--config", str(run_dir / "gateway-config.json"), "--port", str(port)],
                                   cwd=REPOSITORY, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    ownership = {"pid": process.pid, "uid": os.getuid(),
        "start_ticks": Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]}
    write_json(run_dir / "gateway-process.json", ownership)
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("Diagnostic gateway failed; see gateway.log")
            try:
                status = httpx.get(health, timeout=3, trust_env=False)
                if status.status_code == 200 and status.json().get("ready"):
                    write_json(run_dir / "gateway-health.json", status.json())
                    return ownership
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise RuntimeError("Diagnostic local capabilities are not ready")
    except BaseException:
        stop_gateway(ownership)
        raise


def evolve(config: dict, run_dir: Path) -> None:
    from scripts.wildclawbench.evolution import propose_candidates
    marker = run_dir / "native-invocation.json"
    resume = marker.exists()
    if resume and json.loads(marker.read_text())["status"] != "completed":
        return  # An interrupted/failed invocation is a gap, not permission to run another Agent.
    if historical_input(config["all_tasks"]) != config["historical_diagnostic_input"]:
        raise ValueError("Historical evolution evidence changed")
    sessions = [json.loads(Path(item["path"]).read_text())["session"]
                for item in config["historical_diagnostic_input"]["sessions"]]
    write_json(run_dir / "native-evolution-input.json", {
        "source_run": str(OLD_ROOT), "phase": "day", "round": 1,
        "sessions": [{"task_id": s["task_id"], "session_id": s["session_id"],
                      "sha256": canonical_hash(s)} for s in sessions],
        "human_video_contract_injected": False,
    })
    if resume:
        cached = json.loads((run_dir / "native-evolution/evolution/proposal-summary.json").read_text())
        if cached.get("status") != "completed":
            raise ValueError("Completed invocation lacks its original proposal evidence")
        candidates = cached["candidates"]
    else:
        write_json(marker, {"status": "started", "allowed_native_invocations": 1})
        try:
            candidates = propose_candidates(run_dir / "native-evolution", run_dir / "banks/S0", sessions, config)
        except Exception as error:
            write_json(marker, {"status": "failed", "error": f"{type(error).__name__}: {error}"})
            return
        write_json(marker, {"status": "completed", "candidates": len(candidates)})
    results = []
    for candidate in candidates:
        proposed = run_dir / "native-preflight-bank" / candidate["candidate_id"]
        temporary = proposed.with_name(proposed.name + ".building")
        if temporary.exists():
            shutil.rmtree(temporary)
        shutil.copytree(run_dir / "banks/S0", temporary)
        for bundle in candidate["bundles"]:
            if tree_hash(Path(bundle["bundle_dir"])) != bundle["bundle_sha256"]:
                raise ValueError("Native exported bundle changed before preflight")
            destination = temporary / bundle["name"]
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(bundle["bundle_dir"], destination)
        expected = tree_hash(temporary)
        if proposed.exists():
            if tree_hash(proposed) != expected:
                raise ValueError("Existing candidate bank differs from exact atomic changeset")
            shutil.rmtree(temporary)
        else:
            os.replace(temporary, proposed)
        deployment = environment.preflight_candidate_bank(proposed, candidate, config)
        if tree_hash(proposed) != expected:
            raise ValueError("Preflight modified candidate bank")
        results.append({"candidate_id": candidate["candidate_id"], "source_task_ids": candidate["source_task_ids"],
                        "expected_bank_sha256": expected, "deployment": deployment})
    write_json(run_dir / "native-acceptance.json", {
        "candidates": results, "published": False,
        "reason": "diagnostic only; paid candidate A/B and publishing outside this 12-execution acceptance",
    })


def diagnostics(config: dict, tasks: list[dict], run_dir: Path, workers: int) -> None:
    jobs = [(task, arm) for task in tasks if task["short_id"] in SHORT_IDS for arm in ("S0", "S6")]
    # Reverse alternating pair order deterministically; no old-condition score cache.
    jobs.sort(key=lambda job: (SHORT_IDS.index(job[0]["short_id"]),
                              (0 if job[1] == "S0" else 1) ^ (SHORT_IDS.index(job[0]["short_id"]) % 2)))
    def execute(task, arm):
        marker = run_dir / "diagnostic-attempts" / (arm + "-" + task["short_id"] + ".json")
        result_path = run_dir / "executions/diagnostic/00/main" / arm / task["short_id"] / "result.json"
        if marker.exists():
            previous = json.loads(result_path.read_text()) if result_path.exists() else {}
            if previous.get("status") not in {"completed", "pending_score"}:
                return previous
        else:
            write_json(marker, {"status": "started", "short_id": task["short_id"], "arm": arm,
                                "max_agent_attempts": 1})
        result = run_one(task, run_dir / "banks" / arm, run_dir, config, "diagnostic", 0, arm)
        write_json(marker, {"status": "closed", "result_status": result["status"],
                            "result_path": str(result_path), "max_agent_attempts": 1})
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(execute, task, arm) for task, arm in jobs]
        for future in concurrent.futures.as_completed(futures):
            future.result()


def report(config: dict, run_dir: Path) -> dict:
    paths = sorted((run_dir / "executions").glob("diagnostic/*/*/*/*/result.json"))
    results = [json.loads(path.read_text()) for path in paths]
    keys = ("short_id", "arm", "status", "score", "timed_out", "wall_seconds", "error")
    rows = [{key: result.get(key) for key in keys}
            for result in results]
    budget = Path(config["runtime_dir"]) / "paid-api/budget.json"
    summary = {"updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "expected": 12,
               "valid_grades": sum(row["status"] == "completed" and row["score"] is not None for row in rows),
               "results": rows, "paid_api": json.loads(budget.read_text()) if budget.exists() else None,
               "claim": "diagnostic only; historical S6 contains manual revision"}
    native = run_dir / "native-invocation.json"
    proposal = run_dir / "native-evolution/evolution/proposal-summary.json"
    summary["native_invocation"] = json.loads(native.read_text()) if native.exists() else {"status": "not_started"}
    summary["native_outcome"] = json.loads(proposal.read_text()).get("outcome") if proposal.exists() else None
    summary["status_scope"] = "12 diagnostic executions only; native invocation and acceptance reported separately"
    markers = [json.loads(path.read_text()) for path in (run_dir / "diagnostic-attempts").glob("*.json")]
    closed = len(markers) == 12 and all(marker["status"] == "closed" for marker in markers)
    summary["status"] = ("completed" if len(rows) == summary["valid_grades"] == 12 else
                         "finished_with_gaps" if closed else "in_progress")
    write_json(output_root(run_dir, config) / "validation-summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "evolve", "diagnostics", "all", "report"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workers", type=int, default=2, choices=(1, 2, 3, 4))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", args.run_id):
        raise ValueError("run-id must be a safe basename")
    config = load_config(args.config)
    if (config.get("historical") or config.get("read_only")) and args.command != "report":
        raise ValueError("Historical/read-only configurations cannot launch diagnostics")
    if config["paid_api_budget_usd"] != 1 or not config.get("alignment") or config["infrastructure_retries"] != 0:
        raise ValueError("This entry requires the dedicated $1 alignment configuration")
    run_dir = Path(config.get("artifacts_dir", Path(config["runtime_dir"]) / "runs")) / args.run_id
    if config.get("outputs_dir"):
        config["artifact_run_dir"] = str(run_dir)
        config["output_run_dir"] = str(Path(config["outputs_dir"]) / args.run_id)
        config.setdefault("gateway_log_dir", str(Path(config["output_run_dir"]) / "api-evidence"))
    if args.command == "report" and (run_dir / "frozen-protocol.json").exists():
        # Reading an archived run must not attempt to execute or refreeze it
        # with the current implementation after a later repair.
        frozen = json.loads((run_dir / "frozen-protocol.json").read_text())
        print(json.dumps(report(frozen["config"], run_dir), ensure_ascii=False), flush=True)
        return
    global OLD_ROOT
    if config.get("historical_run_dir"):
        OLD_ROOT = Path(config["historical_run_dir"]).resolve()
    if OLD_ROOT is None:
        raise ValueError("Alignment diagnostics require an explicit historical_run_dir")
    with exclusive_run(run_dir):
        config, tasks = prepare(config, run_dir)
        ownership = None
        try:
            if args.command in {"all", "evolve", "diagnostics"}:
                ownership = start_gateway(config, run_dir)
            if args.command == "all":
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    native = pool.submit(evolve, config, run_dir)
                    diagnostics(config, tasks, run_dir, args.workers)
                    native.result()
            if args.command == "evolve":
                evolve(config, run_dir)
            if args.command == "diagnostics":
                diagnostics(config, tasks, run_dir, args.workers)
            print(json.dumps(report(config, run_dir), ensure_ascii=False), flush=True)
        finally:
            stop_gateway(ownership)



if __name__ == "__main__":
    main()
