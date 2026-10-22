"""Watchdog recovery must preserve active work and isolate this run's resources."""
import importlib.util
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.wildclawbench.operations import unattended as ops

def test_successful_requests_prevent_stale_restart(tmp_path):
    clock = [0]
    args = SimpleNamespace(run_dir=tmp_path, poll_seconds=2, stale_seconds=1)
    identity = {"pid": 12345, "uid": os.getuid(), "start_ticks": "1"}
    def sleep(seconds):
        clock[0] += seconds
    with patch.object(ops, "alive", side_effect=[True, True, True, False]), \
         patch.object(ops, "model_progress", side_effect=[5, 5, 6, 7]), \
         patch.object(ops.time, "monotonic", side_effect=lambda: clock[0]), \
         patch.object(ops.time, "sleep", side_effect=sleep), \
         patch.object(ops.os, "killpg") as kill:
        ops.wait_controller(identity, args, {})
    kill.assert_not_called()


def test_true_stall_terminates_owned_group_and_marks_work(tmp_path):
    clock = [0]
    active = [True]
    args = SimpleNamespace(run_dir=tmp_path, poll_seconds=2, stale_seconds=1)
    identity = {"pid": 12345, "uid": os.getuid(), "start_ticks": "1"}
    policy = object()
    def sleep(seconds):
        clock[0] += seconds
    def terminate(pid, sig):
        active[0] = False
    with patch.object(ops, "alive", side_effect=lambda _: active[0]), \
         patch.object(ops, "model_progress", return_value=5), \
         patch.object(ops.time, "monotonic", side_effect=lambda: clock[0]), \
         patch.object(ops.time, "sleep", side_effect=sleep), \
         patch.object(ops.os, "getpgid", return_value=12345), \
         patch.object(ops.os, "killpg", side_effect=terminate) as kill, \
         patch.object(ops.native, "event"), \
         patch.object(ops, "mark_stalled_work") as mark, \
         patch.object(ops, "clean_stalled_tasks") as clean:
        ops.wait_controller(identity, args, {}, policy=policy)
    kill.assert_called_once_with(12345, signal.SIGTERM)
    mark.assert_called_once_with(tmp_path, policy)
    clean.assert_called_once_with(tmp_path, {})


def test_container_cleanup_uses_this_run_labels_only(tmp_path):
    from scripts.wildclawbench import environment
    path = tmp_path / "executions/day/01/main/shared/01.01"
    path.mkdir(parents=True)
    owner = "wcb-" + ops.hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:20]
    listing = SimpleNamespace(returncode=0, stdout=(
        f"own sharedskill-task-own {owner}\n"
        "other sharedskill-task-other wcb-other\n"
        f"unrelated unrelated-name {owner}\n"))
    with patch.object(environment, "_run", side_effect=[listing, SimpleNamespace(returncode=0)]) as docker, \
         patch.object(ops.native, "event"):
        ops.clean_stalled_tasks(tmp_path, {"runtime_dir": str(tmp_path)})
    assert docker.call_count == 2
    assert docker.call_args_list[1].args[0] == ["docker", "rm", "--force", "own"]


def test_stalled_task_identity_is_original_and_completed_task_not_skipped(tmp_path):
    tasks = [{"short_id": "01.01", "task_id": "task-a", "source_sha256": "input-a"},
             {"short_id": "01.02", "task_id": "task-b", "source_sha256": "input-b"}]
    frozen = {"config": {"seed": 42}, "tasks": tasks, "source_hashes": {}}
    (tmp_path / "frozen-protocol.json").write_text(json.dumps(frozen))
    (tmp_path / "state.json").write_text(json.dumps({"status": "day", "round": 1}))
    events = [{"event": "task_started", "phase": "day", "round": 1, "arm": "",
               "candidate_id": "", "task": task["short_id"], "bank_sha256": "bank"} for task in tasks]
    events.append({"event": "task_finished", "phase": "day", "round": 1, "arm": "", "task": "01.02"})
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    policy = ops.Policy(tmp_path, "2026-10-13T00:00:00+00:00")
    with patch.object(ops.native, "event"):
        ops.mark_stalled_work(tmp_path, policy)
        ops.mark_stalled_work(tmp_path, policy)
    records = [json.loads(p.read_text()) for p in (policy.operations / "skips").glob("*.json")]
    assert len(records) == 1
    identity = {"phase": "day", "round": 1, "arm": "", "candidate_id": "", "task_id": "task-a",
                "bank": "bank", "config": ops.native.canonical_hash(frozen), "input": "input-a", "seed": 42}
    assert records[0]["execution_hash"] == ops.native.canonical_hash(identity)
    assert records[0]["score_available"] is False
    assert not Path(records[0]["result_path"]).exists()
