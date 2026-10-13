#!/usr/bin/env python3
"""Auditable error-handling overlay; the frozen controller remains unchanged.

The supervisor waits for the exact existing controller to exit. Workers call
run.main('resume') under its native controller lock and frozen protocol checks.
Only unavailable evidence is skipped; scores, task identities, candidate gates,
and the six rounds remain native. This file is outside freeze's source scan.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import random
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run as native
import reports

AUTHORIZATION = "确保代码会继续完成，因为我要去睡觉了，然后遇到错误可以跳过然后要让pipeline一直跑下去，不能浪费时间"


def read(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def graded(record: dict) -> bool:
    score = record.get("score")
    return (record.get("status") == "completed" and isinstance(score, (int, float))
            and not isinstance(score, bool) and math.isfinite(score) and 0 <= score <= 1)


def process_identity(pid: int) -> dict | None:
    """PID reuse is not survival; inaccessible /proc fails closed."""
    try:
        directory = Path("/proc") / str(pid)
        stat = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return {"pid": pid, "uid": directory.stat().st_uid, "start_ticks": stat[19]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(record: dict | None) -> bool:
    if not record or not all(key in record for key in ("pid", "uid", "start_ticks")):
        raise ValueError("A precise pid/uid/start_ticks controller record is required")
    identity = process_identity(int(record["pid"]))
    return bool(identity and all(str(identity[key]) == str(record[key]) for key in identity))


class CandidateUnavailable(RuntimeError):
    def __init__(self, validation: Path, reason: str):
        super().__init__(reason)
        self.validation = validation


class NightUnavailable(RuntimeError):
    pass


class Policy:
    def __init__(self, run_dir: Path, authorized_at: str):
        self.run_dir = run_dir.resolve()
        self.operations = self.run_dir / "operations/unattended"
        self.source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.metadata = {
            "policy": "unattended-error-handling-v1", "source": str(Path(__file__).resolve()),
            "source_sha256": self.source_hash, "authorization_recorded_at": authorized_at,
            "authorization_text": AUTHORIZATION,
            "scope": "control flow only; native retries, identities, models, scores and acceptance rules unchanged",
        }
        self.one = native.run_one
        self.stage = native.run_stage
        self.night_native = native.night
        self.compare_native = native.outcome_preference
        self.report_native = native.generate

    def save_source(self):
        self.operations.mkdir(parents=True, exist_ok=True)
        snapshot = self.operations / "policies" / (self.source_hash + ".py")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        source = Path(__file__).read_bytes()
        if hashlib.sha256(source).hexdigest() != self.source_hash:
            raise ValueError("Overlay source changed during policy registration")
        if snapshot.exists() and snapshot.read_bytes() != source:
            raise ValueError("Stored overlay source does not match its hash")
        if not snapshot.exists():
            snapshot.write_bytes(source)
        native.write_json(snapshot.with_suffix(".json"), self.metadata)
        native.write_json(self.operations / "policy.json", self.metadata)

    def record(self, key: str, kind: str, **fields) -> dict:
        path = self.operations / "skips" / (key + ".json")
        previous = read(path)
        if previous:
            return previous
        value = {**self.metadata, "timestamp": native.now(), "kind": kind, **fields}
        native.write_json(path, value)
        native.event(self.run_dir, "unattended_skip", skip_kind=kind,
                     **{name: item for name, item in value.items() if name != "kind"})
        return value

    def execution(self, task, bank, config, phase, number, arm="", candidate_id=""):
        identity = {"phase": phase, "round": number, "arm": arm, "candidate_id": candidate_id,
                    "task_id": task["task_id"], "bank": native.tree_hash(bank),
                    "config": config["protocol_hash"], "input": task.get("input_sha256", task["source_sha256"]),
                    "seed": config["seed"]}
        digest = native.canonical_hash(identity)
        path = (self.run_dir / "executions" / phase / f"{number:02d}" / (candidate_id or "main")
                / (arm or "shared") / task["short_id"] / "result.json")
        result = read(path)
        if result and result.get("execution_hash") != digest:
            raise ValueError(f"Refusing cross-bank/config result reuse: {path.parent}")
        return digest, path, result

    def run_one(self, task, bank, run_dir, config, phase, number, arm="", candidate_id=""):
        digest, path, result = self.execution(task, bank, config, phase, number, arm, candidate_id)
        skip = read(self.operations / "skips" / (digest + ".json"))
        if skip:
            if task.get("input_sha256"):
                from provenance import validate_task_inputs
                validate_task_inputs(config, task)
            return result or {"task_id": task["task_id"], "short_id": task["short_id"],
                              "status": "missing", "score": None}
        # The native runner retains its infrastructure retry and rescore behavior.
        try:
            result = self.one(task, bank, run_dir, config, phase, number, arm, candidate_id)
        except Exception as error:
            # Isolate unexpected task failures so this user's later tasks run.
            # Recheck immutable evidence before allowing an operational skip.
            if task.get("input_sha256"):
                from provenance import validate_task_inputs
                validate_task_inputs(config, task)
            if self.execution(task, bank, config, phase, number, arm, candidate_id)[0] != digest:
                raise RuntimeError("Frozen bank changed during failed task") from error
            result = read(path) or {"task_id": task["task_id"], "short_id": task["short_id"],
                                    "status": "missing", "score": None}
            self.record(digest, "task", execution_hash=digest, result_path=str(path), phase=phase,
                        round=number, arm=arm, candidate_id=candidate_id, task_id=task["task_id"],
                        status=result["status"], score_available=False, error=str(error))
        if not graded(result):
            self.record(digest, "task", execution_hash=digest, result_path=str(path), phase=phase,
                        round=number, arm=arm, candidate_id=candidate_id, task_id=task["task_id"],
                        status=result.get("status", "missing"), score_available=False,
                        error=result.get("error", "Native execution did not produce a valid grade"))
        return result

    def run_stage(self, tasks, bank, run_dir, config, phase, number, arm="", candidate_id=""):
        try:
            return self.stage(tasks, bank, run_dir, config, phase, number, arm, candidate_id)
        except RuntimeError as error:
            if "tasks need infrastructure/score repair; resume reuses completed executions" not in str(error):
                raise
            # Read every canonical result after all native user queues finish.
            rows = []
            for task in tasks:
                digest, path, result = self.execution(task, bank, config, phase, number, arm, candidate_id)
                row = result or {"task_id": task["task_id"], "short_id": task["short_id"],
                                 "status": "missing", "score": None}
                if not graded(row):
                    self.record(digest, "task", execution_hash=digest, result_path=str(path), phase=phase,
                                round=number, arm=arm, candidate_id=candidate_id, task_id=task["task_id"],
                                status=row.get("status", "missing"), score_available=False, error=str(error))
                rows.append(row)
            if phase == "validation":
                raise CandidateUnavailable(run_dir / "nights" / f"{number:02d}" / "validation" / candidate_id,
                                           f"validation_unavailable: {error}") from error
            return sorted(rows, key=lambda row: row["short_id"])

    def compare(self, old, new, directory, config):
        try:
            old_map = {row["task_id"]: row for row in old}
            for row in new:
                cached = read(directory / (row["short_id"] + ".json"))
                if cached and (cached.get("baseline_bank") != old_map[row["task_id"]]["skill_bank_sha256"]
                               or cached.get("candidate_bank") != row["skill_bank_sha256"]
                               or cached.get("preference") not in {"candidate", "baseline", "tie"}):
                    raise ValueError("Cached comparison has invalid bank provenance or preference")
            return self.compare_native(old, new, directory, config)
        except Exception as error:
            native.write_json(directory.parent / "comparison-failure.json",
                              {**self.metadata, "timestamp": native.now(),
                               "error": f"{type(error).__name__}: {error}",
                               "baseline_bank": old[0]["skill_bank_sha256"],
                               "candidate_bank": new[0]["skill_bank_sha256"]})
            raise CandidateUnavailable(directory.parent, f"comparison_unavailable: {error}") from error

    def propose(self, directory, bank, sessions, config):
        parent_hash = native.tree_hash(bank)
        try:
            return self.propose_native(directory, bank, sessions, config)
        except Exception as error:
            if native.tree_hash(bank) != parent_hash:
                raise RuntimeError("Proposal modified the immutable parent bank") from error
            raise NightUnavailable(f"{type(error).__name__}: {error}") from error

    def reject(self, number, error):
        validation = error.validation
        candidates = read(self.run_dir / "nights" / f"{number:02d}" / "candidates.json")
        candidate = candidates[int(validation.name.split("-", 1)[0]) - 1]
        path = validation / "decision.json"
        if path.exists():
            raise RuntimeError("Refusing to overwrite an existing candidate decision")
        decision = {"accepted": False, "pending": False, "reason": str(error), "skipped": True,
                    "name": candidate["name"], "candidate_id": validation.name,
                    "source_task_ids": candidate["source_task_ids"],
                    "parent_sha256": native.tree_hash(validation.parent.parent / "working-bank"),
                    "candidate_bank_sha256": native.tree_hash(validation / "candidate-bank"),
                    "bundle_tree_sha256": native.tree_hash(Path(candidate["bundle_dir"])),
                    "timestamp": native.now(), "operations_policy": self.metadata}
        if decision["bundle_tree_sha256"] != candidate["bundle_sha256"]:
            raise ValueError("Candidate bundle changed before unattended rejection")
        native.write_json(path, decision)
        self.record(f"night-{number:02d}-{validation.name}", "candidate", round=number,
                    candidate_id=validation.name, decision_path=str(path), reason=str(error))

    def unchanged_night(self, number, bank, reason):
        directory = self.run_dir / "nights" / f"{number:02d}"
        published = self.run_dir / "banks" / f"S{number}"
        parent_hash = native.tree_hash(bank)
        temporary = published.with_name(published.name + ".publishing")
        if temporary.exists():
            shutil.rmtree(temporary)
        shutil.copytree(bank, temporary)
        if native.tree_hash(temporary) != parent_hash:
            raise RuntimeError("Skipped night snapshot differs from its parent")
        if published.exists():
            if native.tree_hash(published) != parent_hash:
                raise RuntimeError("Existing publication differs from skipped night's parent")
            shutil.rmtree(temporary)
        else:
            os.replace(temporary, published)
        native.write_json(directory / "publication.json", {
            "round": number, "parent_sha256": parent_hash, "bank_sha256": parent_hash,
            "timestamp": native.now(), "candidate_count": 0, "skipped": True,
            "reason": reason, "operations_policy": self.metadata,
        })
        self.record(f"night-{number:02d}", "night", round=number, reason=reason,
                    parent_sha256=parent_hash, bank_sha256=parent_hash)
        native.event(self.run_dir, "night_published", round=number, bank_sha256=parent_hash,
                     candidates=0, skipped=True, reason=reason)
        return published

    def night(self, number, bank, sessions, tasks, run_dir, config):
        stalled = read(self.operations / "skips" / f"night-{number:02d}-stalled.json")
        if stalled and not (run_dir / "nights" / f"{number:02d}" / "publication.json").exists():
            if native.tree_hash(bank) != stalled["parent_sha256"]:
                raise ValueError("Stalled night's parent bank changed")
            return self.unchanged_night(number, bank, stalled.get("reason", "stalled proposal skipped"))
        while True:
            try:
                return self.night_native(number, bank, sessions, tasks, run_dir, config)
            except CandidateUnavailable as error:
                self.reject(number, error)
                # Replay native decisions; completed results and accepted bytes
                # are reused, and the rejected candidate cannot run again.
            except NightUnavailable as error:
                return self.unchanged_night(number, bank, str(error))

    def report(self, run_dir, tasks):
        # Keep every native grade and denominator, with explicit missing rows.
        summary = self.report_native(run_dir, tasks)
        skips = [read(path) for path in sorted((self.operations / "skips").glob("*.json"))]
        summary["operations_policy"] = self.metadata
        summary["unattended_skips"] = skips
        summary["fixed_execution_coverage"] = coverage(run_dir, tasks)
        native.write_json(run_dir / "report/summary.json", summary)
        text = "\n\n无人值守错误处理覆盖层：缺失评分保持为空；跳过不表示 completed。\n"
        text += f"策略源码 SHA256：`{self.source_hash}`；授权登记：`{self.metadata['authorization_recorded_at']}`。\n"
        text += "\n| 固定阶段 | 库／轮次 | 有效评分／固定题数 |\n|---|---|---:|\n"
        for stage in summary["fixed_execution_coverage"]["stages"]:
            text += f"| {stage['phase']} | {stage['arm'] or stage['round']} | {stage['graded']}/{stage['expected']} |\n"
        text += "\n| 跳过类型 | 轮次 | 任务／候选 | 原因 |\n|---|---:|---|---|\n"
        for skip in skips:
            label = skip.get("task_id", skip.get("candidate_id", ""))
            reason = str(skip.get("reason", skip.get("error", ""))).replace("\n", " ").replace("|", "/")
            text += f"| {skip['kind']} | {skip.get('round', '')} | {label} | {reason} |\n"
        report_path = run_dir / "report/REPORT.md"
        if report_path.exists():
            with report_path.open("a") as stream:
                stream.write(text)
        return summary

    def experiment(self, tasks, initial, run_dir, config):
        native.check_services(config)
        if not read(run_dir / "pilot-gate.json", {}).get("passed"):
            # This overlay is for an already approved trial; never manufacture
            # a passed pilot from incomplete grades.
            raise RuntimeError("Unattended resume requires the existing passed pilot")
        bank = initial
        for number in range(1, 7):
            native.update_state(run_dir, "day", round=number, bank_sha256=native.tree_hash(bank))
            rows = native.run_stage(tasks, bank, run_dir, config, "day", number)
            native.update_state(run_dir, "night", round=number)
            sessions = [row["session"] for row in rows if graded(row) and isinstance(row.get("session"), dict)]
            bank = native.night(number, bank, sessions, tasks, run_dir, config)
            native.generate(run_dir, tasks)
        native.update_state(run_dir, "endpoint")

        def endpoint_worker(user_tasks):
            for task in user_tasks:
                arms = [("S0", initial), ("S6", bank)]
                random.Random(config["seed"] + int(task["short_id"].replace(".", ""))).shuffle(arms)
                for arm, selected_bank in arms:
                    native.run_one(task, selected_bank, run_dir, config, "endpoint", 6, arm)

        assignments = [[task for task in tasks if task["user_id"] == f"user-{i + 1:02d}"]
                       for i in range(config["users"])]
        with native.concurrent.futures.ThreadPoolExecutor(max_workers=config["users"]) as pool:
            for future in native.concurrent.futures.as_completed([pool.submit(endpoint_worker, rows) for rows in assignments]):
                future.result()
        fixed = coverage(run_dir, tasks)
        skipped = any((self.operations / "skips").glob("*.json"))
        status = "finished_with_gaps" if fixed["graded"] != fixed["expected"] or skipped else "complete"
        native.update_state(run_dir, status, finished=True, completed_at=native.now(),
                            fixed_execution_coverage=fixed, operations_policy=self.metadata)
        native.generate(run_dir, tasks)
        native.event(run_dir, "experiment_finished", status=status, coverage=fixed)

    def install(self, evolution):
        from unittest.mock import patch
        self.propose_native = evolution.propose_candidates
        stack = contextlib.ExitStack()
        for name, function in (("run_one", self.run_one), ("run_stage", self.run_stage),
                               ("night", self.night), ("outcome_preference", self.compare),
                               ("generate", self.report), ("run_experiment", self.experiment)):
            stack.enter_context(patch.object(native, name, function))
        stack.enter_context(patch.object(evolution, "propose_candidates", self.propose))
        return stack


def coverage(run_dir, tasks):
    stages = []
    frozen = read(run_dir / "frozen-protocol.json")
    for phase, number, arm in [("day", n, "") for n in range(1, 7)] + [("endpoint", 6, "S0"), ("endpoint", 6, "S6")]:
        bank = run_dir / "banks" / (arm if arm else f"S{number - 1}")
        bank_hash = native.tree_hash(bank) if bank.is_dir() else None
        count = 0
        for task in tasks:
            record = read(run_dir / "executions" / phase / f"{number:02d}" / "main"
                          / (arm or "shared") / task["short_id"] / "result.json", {})
            if frozen and record:
                expected = native.canonical_hash({
                    "phase": phase, "round": number, "arm": arm, "candidate_id": "",
                    "task_id": task["task_id"], "bank": bank_hash,
                    "config": native.canonical_hash(frozen),
                    "input": task.get("input_sha256", task["source_sha256"]), "seed": frozen["config"]["seed"],
                })
                if record.get("execution_hash") != expected:
                    raise ValueError("Fixed task result has incompatible frozen execution identity")
            count += graded(record)
        stages.append({"phase": phase, "round": number, "arm": arm, "graded": count,
                       "expected": len(tasks), "grading_coverage": count / len(tasks) if tasks else None})
    return {"expected": len(tasks) * 8, "graded": sum(row["graded"] for row in stages), "stages": stages}


def terminal(run_dir):
    state = read(run_dir / "state.json", {})
    if state.get("status") not in {"complete", "finished_with_gaps"}:
        return False
    tasks = read(run_dir / "frozen-protocol.json")["tasks"]
    for number in range(1, 7):
        publication = read(run_dir / "nights" / f"{number:02d}" / "publication.json", {})
        if not publication or not (run_dir / "banks" / f"S{number}").is_dir():
            return False
        if (publication["bank_sha256"] != native.tree_hash(run_dir / "banks" / f"S{number}")
                or publication["parent_sha256"] != native.tree_hash(run_dir / "banks" / f"S{number - 1}")):
            raise ValueError("Terminal publication or parent hash mismatch")
    fixed = coverage(run_dir, tasks)
    if state["status"] == "complete":
        return fixed["graded"] == fixed["expected"]
    if not state.get("finished"):
        return False
    # Every missing fixed grade must have an explicit durable skip declaration.
    skip_paths = {item.get("result_path") for path in (run_dir / "operations/unattended/skips").glob("*.json")
                  if (item := read(path)).get("kind") == "task"}
    for stage in fixed["stages"]:
        for task in tasks:
            path = (run_dir / "executions" / stage["phase"] / f"{stage['round']:02d}" / "main"
                    / (stage["arm"] or "shared") / task["short_id"] / "result.json")
            if not graded(read(path, {})) and str(path) not in skip_paths:
                return False
    return True


def worker(arguments):
    import evolution
    policy = Policy(arguments.run_dir, arguments.authorized_at)
    worker_command = [sys.executable, *sys.argv]
    if arguments.source_sha256 and policy.source_hash != arguments.source_sha256:
        raise ValueError("Overlay source changed after supervisor authorization")
    original_lock = native.exclusive_run

    @contextlib.contextmanager
    def locked(run_dir):
        with original_lock(run_dir):
            previous = read(run_dir / "controller-process.json")
            stamp = native.now().replace(":", "").replace("-", "")
            archive = policy.operations / "controllers" / (stamp + ".json")
            if previous:
                native.write_json(archive, previous)
            policy.save_source()
            native.write_json(run_dir / "controller-process.json", {
                **process_identity(os.getpid()), "started_at": native.now(), "run_dir": str(run_dir),
                "command": worker_command, "native_resume_command": [sys.executable, *sys.argv],
                "log": str(policy.operations / "worker.log"),
                "operation": "unattended native resume with explicit skip overlay",
                "previous_controller_record": str(archive), "operations_policy": policy.metadata,
            })
            native.event(run_dir, "unattended_worker_started", **policy.metadata)
            yield

    from unittest.mock import patch
    with policy.install(evolution), patch.object(native, "exclusive_run", locked), patch.object(
        sys, "argv", [str(ROOT / "run.py"), "resume", "--config", str(arguments.config),
                      "--run-dir", str(arguments.run_dir)]
    ):
        native.main()
    if not terminal(arguments.run_dir):
        raise RuntimeError("Worker returned without a verified terminal state")


def model_progress(config):
    try:
        response = native.httpx.get(config["model_base_url"].removesuffix("/v1") + "/metrics",
                                    timeout=3, trust_env=False)
        response.raise_for_status()
        values = [float(line.rsplit(None, 1)[1]) for line in response.text.splitlines()
                  if line.startswith("vllm:request_success_total{")]
        return sum(values) if values else None
    except (native.httpx.HTTPError, ValueError):
        return None


def clean_stalled_tasks(run_dir, config):
    """Remove only unfinished tasks in this run, using their exact Docker labels."""
    import environment
    owners = {"wcb-" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:20]
              for path in (run_dir / "executions").glob("*/*/*/*/*")
              if path.is_dir() and not graded(read(path / "result.json", {}))}
    listing = environment._run(
        ["docker", "ps", "--all", "--no-trunc", "--format",
         '{{.ID}} {{.Names}} {{.Label "org.sharedskill.run_id"}}'],
        config=config, timeout=20, check=False)
    removed = []
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[1].startswith("sharedskill-task-") or parts[2] not in owners:
            continue
        outcome = environment._run(["docker", "rm", "--force", parts[0]],
                                   config=config, timeout=30, check=False)
        removed.append({"container": parts[1], "run_id": parts[2], "exit_code": outcome.returncode})
    native.event(run_dir, "unattended_stalled_task_cleanup", containers=removed)


def mark_stalled_work(run_dir, policy):
    frozen = read(run_dir / "frozen-protocol.json")
    tasks = {task["short_id"]: task for task in frozen["tasks"]}
    active = {}
    for line in (run_dir / "events.jsonl").read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") not in {"task_started", "task_finished"}:
            continue
        key = tuple(event.get(field, "") for field in ("phase", "round", "arm", "task"))
        if event["event"] == "task_started":
            active[key] = event
        else:
            active.pop(key, None)
    unfinished = 0
    for event in active.values():
        task = tasks[event["task"]]
        identity = {"phase": event["phase"], "round": event["round"], "arm": event.get("arm", ""),
                    "candidate_id": event.get("candidate_id", ""), "task_id": task["task_id"],
                    "bank": event["bank_sha256"], "config": native.canonical_hash(frozen),
                    "input": task.get("input_sha256", task["source_sha256"]), "seed": frozen["config"]["seed"]}
        digest = native.canonical_hash(identity)
        path = (run_dir / "executions" / identity["phase"] / f"{identity['round']:02d}"
                / (identity["candidate_id"] or "main") / (identity["arm"] or "shared")
                / task["short_id"] / "result.json")
        result = read(path, {})
        if graded(result) or read(policy.operations / "skips" / (digest + ".json")):
            continue
        if result and result.get("execution_hash") != digest:
            raise ValueError("Stalled execution identity differs from its stored result")
        policy.record(digest, "task", execution_hash=digest, result_path=str(path),
                      phase=identity["phase"], round=identity["round"], arm=identity["arm"],
                      candidate_id=identity["candidate_id"], task_id=task["task_id"],
                      status=result.get("status", "missing"), score_available=False,
                      error="Controller exceeded the no-progress watchdog budget")
        unfinished += 1
    state = read(run_dir / "state.json", {})
    if not unfinished and state.get("status") == "night":
        number = state["round"]
        if not (run_dir / "nights" / f"{number:02d}" / "candidates.json").exists():
            policy.record(f"night-{number:02d}-stalled", "night", round=number,
                          parent_sha256=native.tree_hash(run_dir / "banks" / f"S{number - 1}"),
                          reason="Night proposal exceeded the no-progress watchdog budget")


def wait_controller(controller, arguments, config, process=None, policy=None):
    last_progress = time.monotonic()
    stamps = None
    successes = model_progress(config)
    while alive(controller) and (process is None or process.poll() is None):
        current = tuple((arguments.run_dir / name).stat().st_mtime_ns
                        if (arguments.run_dir / name).exists() else None
                        for name in ("events.jsonl", "state.json"))
        count = model_progress(config)
        if current != stamps or (count is not None and (successes is None or count > successes)):
            last_progress = time.monotonic()
        stamps = current
        if count is not None:
            successes = count
        if time.monotonic() - last_progress >= arguments.stale_seconds:
            if not alive(controller) or int(controller["uid"]) != os.getuid():
                return
            pid = int(controller["pid"])
            if os.getpgid(pid) != pid:
                raise RuntimeError("Stalled controller is not an isolated process-group leader")
            native.event(arguments.run_dir, "unattended_stalled_controller", controller=controller,
                         stale_seconds=arguments.stale_seconds)
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 30
            while alive(controller) and time.monotonic() < deadline:
                time.sleep(1)
            if alive(controller):
                os.killpg(pid, signal.SIGKILL)
            if policy is not None:
                mark_stalled_work(arguments.run_dir, policy)
            try:
                clean_stalled_tasks(arguments.run_dir, config)
            except Exception as error:
                native.event(arguments.run_dir, "unattended_cleanup_error", error=f"{type(error).__name__}: {error}")
            return
        time.sleep(arguments.poll_seconds)


def supervisor(arguments):
    run_dir = arguments.run_dir
    operations = run_dir / "operations/unattended"
    operations.mkdir(parents=True, exist_ok=True)
    # Separate from controller.lock: waiting never interferes with native work.
    with (operations / "supervisor.lock").open("a") as lock:
        native.fcntl.flock(lock, native.fcntl.LOCK_EX | native.fcntl.LOCK_NB)
        policy = Policy(run_dir, arguments.authorized_at)
        policy.save_source()
        config = native.load_config(arguments.config)
        native.write_json(operations / "supervisor-process.json", {
            **process_identity(os.getpid()), "started_at": native.now(), "run_dir": str(run_dir),
            "command": [sys.executable, *sys.argv], "operations_policy": policy.metadata,
            "poll_seconds": arguments.poll_seconds, "stale_seconds": arguments.stale_seconds,
            "termination_scope": "Exact owned controller identity and its isolated process group only",
            "progress_sources": ["task/state events", "successful requests to this run's local model"],
        })
        original = read(run_dir / "controller-process.json")
        native.write_json(operations / "waiting-for-controller.json", original)
        native.event(run_dir, "unattended_supervisor_waiting", controller=original, **policy.metadata)
        failures = 0
        while True:
            # Re-read on every restart: another native controller may have
            # acquired its lock in between, and must also be allowed to finish.
            controller = read(run_dir / "controller-process.json")
            wait_controller(controller, arguments, config, policy=policy)
            if terminal(run_dir):
                native.event(run_dir, "unattended_supervisor_finished", state=read(run_dir / "state.json"))
                return
            command = [sys.executable, str(Path(__file__).resolve()), "worker", "--run-dir", str(run_dir),
                       "--config", str(arguments.config), "--authorized-at", arguments.authorized_at,
                       "--source-sha256", policy.source_hash]
            with (operations / "worker.log").open("a") as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                native.event(run_dir, "unattended_worker_launched", pid=process.pid, command=command)
                identity = process_identity(process.pid)
                if identity:
                    wait_controller(identity, arguments, config, process, policy)
                process.wait()
            if terminal(run_dir):
                native.event(run_dir, "unattended_supervisor_finished", exit_code=process.returncode,
                             state=read(run_dir / "state.json"))
                return
            failures += 1
            delay = min(300, 10 * 2 ** min(failures - 1, 5))
            native.event(run_dir, "unattended_worker_restart", exit_code=process.returncode,
                         backoff_seconds=delay, restart=failures)
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                time.sleep(min(arguments.poll_seconds, deadline - time.monotonic()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["supervisor", "worker"])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--authorized-at", required=True,
                        help="UTC time authorization was locally recorded, not a claimed user-message timestamp")
    parser.add_argument("--source-sha256")
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--stale-seconds", type=float, default=7200,
                        help="Restart only after no task events and no successful model requests for this interval")
    arguments = parser.parse_args()
    arguments.run_dir = arguments.run_dir.resolve()
    arguments.config = arguments.config.resolve()
    if not 1 <= arguments.poll_seconds <= 30:
        parser.error("--poll-seconds must be between 1 and 30")
    if arguments.stale_seconds < 3600:
        parser.error("--stale-seconds must be at least 3600")
    if not (arguments.run_dir / "frozen-protocol.json").is_file():
        parser.error("--run-dir must name an existing frozen run")
    timestamp = native.dt.datetime.fromisoformat(arguments.authorized_at.replace("Z", "+00:00"))
    if timestamp.utcoffset() != native.dt.timedelta(0):
        parser.error("--authorized-at must include the UTC offset")
    {"supervisor": supervisor, "worker": worker}[arguments.command](arguments)


if __name__ == "__main__":
    main()
